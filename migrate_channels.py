#!/usr/bin/env python3
"""
migrate_channels.py - one-time backfill for transcripts grabbed BEFORE the
per-channel-subfolder + Channel-header change.

For every *.txt sitting flat in transcripts/:
  * read the video id from its "URL:" header line
  * look up the channel via oEmbed (author_name) through YOUTUBE_BIND_IP
  * insert a "Channel: <name>" line under the Title line (if missing)
  * move the file into transcripts/<channel>/

Idempotent: files already inside a channel subfolder are left alone, so it's
safe to re-run. Reuses grab_transcripts.py's helpers - no new fetch logic.
"""
import time
from pathlib import Path

import grab_transcripts as g

BASE = Path(__file__).resolve().parent
OEMBED_DELAY = 0.5   # gentle pause between metadata lookups


def video_id_from_header(lines):
    for line in lines[:10]:
        if line.startswith("URL:"):
            return g.extract_video_id(line.split("URL:", 1)[1].strip())
    return None


def main():
    ip = g.bind_ip()
    out_dir = BASE / g.OUTPUT_DIR
    flat = sorted(p for p in out_dir.glob("*.txt") if p.is_file())
    print(f"egress : {'bound ' + ip if ip else 'default route'}")
    print(f"flat transcripts to migrate: {len(flat)}\n")

    cache = {}
    moved = skipped = unknown = 0
    for p in flat:
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        vid = video_id_from_header(lines)
        if not vid:
            print(f"  SKIP (no URL/id in header): {p.name}")
            skipped += 1
            continue

        if vid not in cache:
            _title, channel = g.fetch_meta(vid, ip)
            cache[vid] = channel or "Unknown Channel"
            time.sleep(OEMBED_DELAY)
        channel = cache[vid]
        if channel == "Unknown Channel":
            unknown += 1

        # insert "Channel:" right under "Title:" if not already present
        if not any(line.startswith("Channel:") for line in lines[:10]):
            new_lines, placed = [], False
            for line in lines:
                new_lines.append(line)
                if not placed and line.startswith("Title:"):
                    new_lines.append(f"Channel: {channel}")
                    placed = True
            if not placed:
                new_lines.insert(0, f"Channel: {channel}")
            text = "\n".join(new_lines)

        cdir = out_dir / g.sanitize_filename(channel, "Unknown Channel")
        cdir.mkdir(parents=True, exist_ok=True)
        target = cdir / p.name
        if target.exists() and target.resolve() != p.resolve():
            target = cdir / f"{p.stem} [{vid}]{p.suffix}"
        target.write_text(text, encoding="utf-8")
        if target.resolve() != p.resolve():
            p.unlink()
        print(f"  [{channel}]  {p.name}")
        moved += 1

    print(f"\ndone: moved {moved} | skipped {skipped} | unknown-channel {unknown}")


if __name__ == "__main__":
    main()
