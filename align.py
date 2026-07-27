#!/usr/bin/env python
# coding: utf-8
"""Map chat/like/social/stats timestamps <-> recorded video position, using the
wall-clock<->out_time sidecar (timing_*.csv) written by the recorder.

Usage:
  # tag a data CSV with (segment_file, video_pts_s, in_gap):
  python align.py tag  <session_dir> [chat|like|social|stats]

  # look up where a wall-clock moment lands in the video:
  python align.py at   <session_dir> "2026-07-26 18:45:03"

  # extract the video frame for a wall-clock moment (needs ffmpeg):
  python align.py frame <session_dir> "2026-07-26 18:45:03" [out.jpg]
"""
import csv, os, sys, glob, subprocess
from datetime import datetime


def load_timing(session_dir):
    """Return sorted list of (wall_epoch, segment_stem, seek_s).

    seek_s is normalized to 0-based per segment (video_pts_s - segment's first pts),
    because auto-convert produces a zero-based .mp4 while the raw mpegts out_time
    carries the stream's PTS offset. So seek_s == the correct `-ss` into the .mp4.
    """
    raw = []
    t0 = {}   # stem -> min video_pts_s seen
    for f in glob.glob(os.path.join(session_dir, 'timing_*.csv')):
        with open(f, encoding='utf-8') as fp:
            for r in csv.DictReader(fp):
                stem = os.path.splitext(r['segment_file'])[0]   # match .ts or .mp4
                pts = float(r['video_pts_s'])
                raw.append((float(r['wall_epoch']), stem, pts))
                t0[stem] = min(t0.get(stem, pts), pts)
    rows = [(w, s, round(p - t0[s], 3)) for (w, s, p) in raw]
    rows.sort()
    return rows


def resolve_media(session_dir, stem):
    """Prefer the converted .mp4, fall back to .ts."""
    for ext in ('.mp4', '.ts', '.flv'):
        p = os.path.join(session_dir, stem + ext)
        if os.path.exists(p):
            return p
    return os.path.join(session_dir, stem + '.mp4')


def data_to_video(T, timing):
    """wall epoch T -> (segment_stem, video_pts_s, in_gap) or None if outside coverage."""
    for i in range(len(timing) - 1):
        w0, s0, p0 = timing[i]
        w1, s1, p1 = timing[i + 1]
        if s0 == s1 and w0 <= T <= w1:
            frac = (T - w0) / (w1 - w0) if w1 > w0 else 0.0
            in_gap = (p1 - p0) < 0.05          # out_time frozen => this instant was lost
            return s0, round(p0 + frac * (p1 - p0), 3), in_gap
    return None


def parse_time(s):
    s = s.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    # epoch fallback
    return float(s)


def tag_csv(session_dir, kind, timing):
    """Tag one data CSV. Returns (hit, total) or None if the source is missing."""
    src = os.path.join(session_dir, f'{kind}.csv')
    if not os.path.exists(src):
        return None
    out = os.path.join(session_dir, f'{kind}_aligned.csv')
    n = hit = 0
    with open(src, encoding='utf-8-sig') as fi, open(out, 'w', newline='', encoding='utf-8-sig') as fo:
        rd = csv.DictReader(fi)
        w = csv.writer(fo)
        w.writerow(rd.fieldnames + ['segment_file', 'video_pts_s', 'in_gap'])
        for row in rd:
            n += 1
            m = data_to_video(parse_time(row['time']), timing)
            if m:
                hit += 1
                seg = resolve_media(session_dir, m[0])
                extra = [os.path.basename(seg), m[1], m[2]]
            else:
                extra = ['', '', 'outside']
            w.writerow([row[k] for k in rd.fieldnames] + extra)
    return hit, n


# stats has no per-user 'time' string? it does ('time' col). Tag the standard streams.
_TAGGABLE = ('chat', 'like', 'social', 'stats', 'gift', 'lucky_bag', 'member', 'emoji')


def tag_all(session_dir, log=None):
    """Tag every data CSV present against the timing sidecar. Safe to call at
    session end: returns quietly if there's no timing file (e.g. record disabled).
    Returns dict {kind: (hit, total)}."""
    timing = load_timing(session_dir)
    if not timing:
        return {}
    out = {}
    for kind in _TAGGABLE:
        r = tag_csv(session_dir, kind, timing)
        if r:
            out[kind] = r
            if log:
                log(f"[对齐] {kind}: {r[0]}/{r[1]} 条已映射到视频 → {kind}_aligned.csv")
    return out


def cmd_tag(session_dir, kind):
    timing = load_timing(session_dir)
    if not timing:
        sys.exit(f"no timing_*.csv in {session_dir}")
    r = tag_csv(session_dir, kind, timing)
    if r is None:
        sys.exit(f"no {kind}.csv in {session_dir}")
    print(f"wrote {os.path.join(session_dir, kind+'_aligned.csv')}: "
          f"{r[0]}/{r[1]} rows mapped into video ({r[1]-r[0]} outside coverage)")


def cmd_at(session_dir, when):
    timing = load_timing(session_dir)
    m = data_to_video(parse_time(when), timing)
    if not m:
        sys.exit(f"{when} is outside recorded coverage")
    seg = resolve_media(session_dir, m[0])
    print(f"{when}  ->  {os.path.basename(seg)} @ {m[1]}s" + ("  [IN GAP]" if m[2] else ""))
    print(f"  ffmpeg -ss {m[1]} -i '{seg}' -frames:v 1 -q:v 2 frame.jpg")
    return seg, m[1]


def cmd_frame(session_dir, when, out='frame.jpg'):
    seg, pts = cmd_at(session_dir, when)
    r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(pts),
                        '-i', seg, '-frames:v', '1', '-q:v', '2', out])
    if r.returncode == 0 and os.path.exists(out):
        print(f"saved {out}")
    else:
        sys.exit("frame extraction failed")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    action, sess = sys.argv[1], sys.argv[2]
    if action == 'tag':
        cmd_tag(sess, sys.argv[3] if len(sys.argv) > 3 else 'chat')
    elif action == 'at':
        cmd_at(sess, sys.argv[3])
    elif action == 'frame':
        cmd_frame(sess, sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else 'frame.jpg')
    else:
        print(__doc__); sys.exit(1)
