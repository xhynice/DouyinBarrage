#!/usr/bin/env python3
"""
fleet/pack.py — package one station-night into ModelScope upload artifacts.

Produces, under --out-dir (a git working tree), the layout designed for the douyin dataset:

    video/{date}/{station}/{date}_{station}_shard{NN}.tar   # bin-packed rooms, each shard <= --shard-gb
    text/{date}/{station}.tar.gz                            # all rooms' csv/meta/db/logs (small, kept local too)
    manifest/{date}/{station}.json                          # room->shard index + sha256 + brief

Rooms are keyed by numeric room_id (from each anchor dir's meta.json), never the display name.
Video is bin-packed whole-room (never split) into <=7GB shards. Text is one gzip bundle.

Usage:
    python fleet/pack.py --station st01 --date 20260727 --after 1920 \
        --data-dir data --out-dir /path/to/douyin
"""
import argparse, hashlib, io, json, os, sys, tarfile, time
from pathlib import Path

VIDEO_EXT = {".mp4", ".ts", ".flv"}
# text/sidecar files that travel in the text bundle (everything that is not bulky video)
TEXT_SUFFIXES = (".csv", ".json")
DB_SUFFIXES = (".db", ".db-wal", ".db-shm")


def sha256_file(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_rows(path):
    """data rows in a csv (excludes header); 0 if absent/empty."""
    if not path.exists():
        return 0
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return max(0, n - 1)


def discover(data_dir, date, after):
    """Return list of room dicts for sessions of this date at/after HHMM `after`."""
    rooms = []
    for anchor_dir in sorted(p for p in Path(data_dir).iterdir() if p.is_dir()):
        meta_path = anchor_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        room_id = str(meta.get("room_id") or meta.get("live_id"))
        live_id = str(meta.get("live_id", ""))
        name = meta.get("anchor_name", anchor_dir.name)
        # session dirs like 20260727_1924 belonging to this date, HHMM >= after
        sessions = []
        for s in sorted(anchor_dir.iterdir()):
            if not s.is_dir() or not s.name.startswith(date + "_"):
                continue
            try:
                hhmm = int(s.name.split("_", 1)[1][:4])
            except (IndexError, ValueError):
                continue
            if hhmm >= after:
                sessions.append(s)
        if not sessions:
            continue

        video_files, text_files, video_bytes, chat_rows = [], [], 0, 0
        for s in sessions:
            for f in sorted(s.rglob("*")):
                if f.is_dir():
                    continue
                if f.suffix.lower() in VIDEO_EXT:
                    video_files.append(f)
                    video_bytes += f.stat().st_size
                else:  # csv, json, db, wal, shm, logs/* -> text bundle
                    text_files.append(f)
            chat_rows += csv_rows(s / "chat.csv")

        outcome = "recorded" if video_files and chat_rows else \
                  ("error" if not video_files and not chat_rows else "partial")
        rooms.append(dict(room_id=room_id, live_id=live_id, name=name,
                          anchor_dir=anchor_dir, sessions=sessions,
                          video_files=video_files, text_files=text_files,
                          video_bytes=video_bytes, chat_rows=chat_rows,
                          outcome=outcome))
    return rooms


def ffd_shards(rooms, shard_bytes):
    """First-fit-decreasing bin-packing of whole rooms (by video_bytes) into <=shard_bytes bins.
       Rooms with no video are skipped (text-only rooms carry no shard)."""
    bins = []  # each: {"rooms": [...], "bytes": int}
    for r in sorted((x for x in rooms if x["video_bytes"] > 0),
                    key=lambda x: x["video_bytes"], reverse=True):
        placed = False
        for b in bins:
            if b["bytes"] + r["video_bytes"] <= shard_bytes:
                b["rooms"].append(r); b["bytes"] += r["video_bytes"]; placed = True; break
        if not placed:
            # a single room larger than a shard still gets its own shard (won't happen at SD)
            bins.append({"rooms": [r], "bytes": r["video_bytes"]})
    return bins


def add_to_tar(tar, file_path, arcname):
    tar.add(str(file_path), arcname=arcname)


def rel_arc(room, f):
    """archive path inside a tar: {room_id}/{path-relative-to-anchor-dir}"""
    return f"{room['room_id']}/{f.relative_to(room['anchor_dir'])}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", required=True)
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--after", type=int, default=0, help="only sessions with HHMM >= this")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", required=True, help="git working tree root")
    ap.add_argument("--shard-gb", type=float, default=7.0)
    args = ap.parse_args()

    shard_bytes = int(args.shard_gb * (1 << 30))
    st, date = args.station, args.date
    out = Path(args.out_dir)
    vdir = out / "video" / date / st
    tdir = out / "text" / date
    mdir = out / "manifest" / date
    for d in (vdir, tdir, mdir):
        d.mkdir(parents=True, exist_ok=True)

    rooms = discover(args.data_dir, date, args.after)
    if not rooms:
        print(f"[pack] no sessions for date={date} after={args.after} in {args.data_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"[pack] {len(rooms)} rooms: " +
          ", ".join(f"{r['name']}({r['outcome']},{r['video_bytes']//(1<<20)}MB)" for r in rooms))

    # ---- video shards ----
    bins = ffd_shards(rooms, shard_bytes)
    shard_records = []
    room_to_shard = {}
    for i, b in enumerate(bins, 1):
        shard_name = f"{date}_{st}_shard{i:02d}.tar"
        shard_path = vdir / shard_name
        with tarfile.open(shard_path, "w") as tar:  # no compression: H.264 already compressed
            for r in b["rooms"]:
                for f in r["video_files"]:
                    add_to_tar(tar, f, rel_arc(r, f))
                room_to_shard[r["room_id"]] = shard_name
        sz = shard_path.stat().st_size
        shard_records.append(dict(file=shard_name, bytes=sz, sha256=sha256_file(shard_path),
                                  rooms=[r["room_id"] for r in b["rooms"]]))
        print(f"[pack] {shard_name}: {sz/(1<<30):.2f} GB, {len(b['rooms'])} rooms")

    # ---- text bundle ----
    text_name = f"{st}.tar.gz"
    text_path = tdir / text_name
    with tarfile.open(text_path, "w:gz") as tar:
        for r in rooms:
            for f in r["text_files"]:
                add_to_tar(tar, f, rel_arc(r, f))
    text_rec = dict(file=f"{date}/{text_name}", bytes=text_path.stat().st_size,
                    sha256=sha256_file(text_path))
    print(f"[pack] text bundle: {text_path.stat().st_size/(1<<20):.1f} MB")

    # ---- manifest / brief ----
    manifest = dict(
        station=st, date=date,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        idle=None,  # set by postrun after asserting recorder exited
        shards=shard_records,
        text_bundle=text_rec,
        rooms=[dict(room_id=r["room_id"], live_id=r["live_id"], name=r["name"],
                    shard=room_to_shard.get(r["room_id"]),
                    video_bytes=r["video_bytes"], video_files=len(r["video_files"]),
                    chat_rows=r["chat_rows"], outcome=r["outcome"], transcribed=False)
               for r in rooms],
        summary=dict(rooms=len(rooms),
                     recorded=sum(r["outcome"] == "recorded" for r in rooms),
                     partial=sum(r["outcome"] == "partial" for r in rooms),
                     error=sum(r["outcome"] == "error" for r in rooms),
                     video_gb=round(sum(r["video_bytes"] for r in rooms) / (1 << 30), 2),
                     shards=len(shard_records)),
        upload="pending",
    )
    manifest_path = mdir / f"{st}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[pack] manifest: {manifest_path}")
    print(f"[pack] DONE  {manifest['summary']}")


if __name__ == "__main__":
    main()
