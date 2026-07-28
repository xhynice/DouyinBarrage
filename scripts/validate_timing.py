#!/usr/bin/env python
# coding: utf-8
"""Burn-in validation of the timeline sidecar (multi-room, reconnect-aware).

For each room: pull the live stream with the wall-clock BURNED into every frame
(drawtext %{localtime}) + input -reconnect flags (so transient drops become
intra-file gaps, like the real recorder), while building the SAME wall<->out_time
sidecar. Afterwards it detects gaps (where out_time freezes while wall advances)
and extracts frames at drift points + right after each gap, printing W_map (the
wall-clock the sidecar assigns). Compare W_map to the clock burned on each frame.

Usage: python scripts/validate_timing.py <room1,room2,...> <seconds>
"""
import os, sys, time, threading, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for v in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    os.environ.pop(v, None)
import requests
from service.network import fetch_ttwid, enter_room_api, resolve_live_id, build_http_headers
from base.utils import USER_AGENTS, extract_ua_version
from base.stream import _build_ordered_list

UA = USER_AGENTS[0]; UAV = extract_ua_version(UA)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
ROOMS = (sys.argv[1] if len(sys.argv) > 1 else "56697889278").split(",")
DUR   = int(sys.argv[2]) if len(sys.argv) > 2 else 1800
WORK  = "/tmp/valrun"
GAP_THRESH = 2.0   # wall advanced >= this many s more than out_time => a stall/gap


def get_flv(room):
    s = requests.Session(); s.trust_env = False; s.proxies = {}
    s.headers.update(build_http_headers(UA, UAV))
    lid = resolve_live_id(room, s); ttwid, _ = fetch_ttwid(s, lid, {})
    info = enter_room_api(ttwid, UA, UAV, lid, session=s)
    if info.get("status") != 2:
        return None, None
    flv = (info.get("stream_url") or {}).get("flv_pull_url", {}) or {}
    o = _build_ordered_list(flv)
    return (o[-1] if o else None), (info.get("anchor_name") or room)


def reader(proc, path):
    out_us = None
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("wall_epoch,wall_iso,video_pts_s\n")
        for raw in iter(proc.stdout.readline, b''):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us="):
                v = line.split("=", 1)[1]; out_us = int(v) if v.lstrip("-").isdigit() else None
            elif line.startswith("progress="):
                if out_us is not None and out_us >= 0:
                    now = time.time()
                    iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int((now%1)*1000):03d}"
                    fp.write(f"{now:.3f},{iso},{out_us/1e6:.3f}\n"); fp.flush()
                out_us = None
                if line == "progress=end":
                    break


def capture(room):
    url, name = get_flv(room)
    if not url:
        print(f"  {room}: not live, skipping"); return None
    ts = os.path.join(WORK, f"burnin_{room}.ts")
    tim = os.path.join(WORK, f"timing_{room}.csv")
    vf = (f"drawtext=fontfile={FONT}:text=%{{localtime}}:x=20:y=20:fontsize=40:"
          f"fontcolor=yellow:box=1:boxcolor=black@0.7")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-user_agent", UA,
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1",
           "-reconnect_delay_max", "30",
           "-re", "-i", url,
           "-vf", vf,
           "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
           "-crf", "30", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
           "-progress", "pipe:1", "-f", "mpegts", ts]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    th = threading.Thread(target=reader, args=(proc, tim), daemon=True); th.start()
    print(f"  {room} ({name}): recording -> {ts}")
    return {"room": room, "name": name, "ts": ts, "tim": tim, "proc": proc, "th": th}


def load(tim):
    rows = []
    for ln in open(tim, encoding="utf-8").read().splitlines()[1:]:
        we, wi, pts = ln.split(","); rows.append((float(we), wi, float(pts)))
    return rows


def find_gaps(rows):
    gaps = []
    for i in range(len(rows) - 1):
        dwall = rows[i+1][0] - rows[i][0]
        dpts  = rows[i+1][2] - rows[i][2]
        if dwall - dpts >= GAP_THRESH:
            gaps.append((rows[i][2], round(dwall - dpts, 1), i+1))  # (pts_before, gap_s, idx_after)
    return gaps


def grab(ts, seek, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", ts, "-ss", f"{seek:.3f}",
                    "-frames:v", "1", "-q:v", "2", out], check=False)


def main():
    os.makedirs(WORK, exist_ok=True)
    print(f"=== capturing {len(ROOMS)} rooms for {DUR}s ===")
    caps = [c for c in (capture(r) for r in ROOMS) if c]
    if not caps:
        print("no live rooms"); return
    time.sleep(DUR)
    print("=== stopping ===")
    for c in caps:
        try: c["proc"].stdin.write(b"q\n"); c["proc"].stdin.close()
        except Exception: pass
    for c in caps:
        try: c["proc"].wait(timeout=30)
        except Exception:
            c["proc"].kill()
        c["th"].join(timeout=5)

    print("\n=== analysis ===")
    samples = []
    for c in caps:
        rows = load(c["tim"])
        if len(rows) < 5:
            print(f"{c['name']}: too few rows"); continue
        t0 = rows[0][2]; span = rows[-1][2] - rows[0][2]
        gaps = find_gaps(rows)
        print(f"\n{c['name']} ({c['room']}): {len(rows)} rows, span {span:.0f}s, gaps={len(gaps)}")
        for g in gaps:
            print(f"    gap ~{g[1]}s at out_time {g[0]:.1f}s")
        # drift points: start / mid / end
        pts_pick = [rows[0][2] + f*span for f in (0.10, 0.50, 0.90)]
        tag = ["drift"] * 3
        # + a frame right AFTER each gap (the seam case)
        for g in gaps[:4]:
            pts_pick.append(rows[g[2]][2]); tag.append(f"post-gap(+{g[1]}s)")
        for k, (tp, tg) in enumerate(zip(pts_pick, tag)):
            r = min(rows, key=lambda x: abs(x[2] - tp))
            seek = r[2] - t0
            img = os.path.join(WORK, f"f_{c['room']}_{k}.jpg")
            grab(c["ts"], seek, img)
            samples.append((c["name"], tg, seek, r[1], img))

    print(f"\n{'room':<14} {'kind':<16} {'seek':>7} {'W_map (sidecar wall)':>24}   frame")
    for nm, tg, sk, wm, img in samples:
        print(f"{nm:<14} {tg:<16} {sk:>7.1f} {wm:>24}   {img}")
    print("\nCompare each frame's on-screen clock (W_burn) to W_map. Seam proof = post-gap rows match.")


if __name__ == "__main__":
    main()
