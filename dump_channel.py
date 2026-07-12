#!/usr/bin/env python3
"""
dump_channel.py - list every video on a YouTube channel to a paste-ready text file.

One fast, flat pass over the channel's videos tab (browse endpoint - NOT the
rate-limited caption endpoint), so it's cheap and safe to run.

Output is directly compatible with yt-grab's links.txt: each video is a
"# [duration] Title" comment line followed by its watch URL. Copy the whole file
straight in (yt-grab skips the # title lines), or eyeball it and grab the ones
you want.

Usage:
    python dump_channel.py [CHANNEL_URL ...]
    (no args -> the DEFAULT_URL below)

Accepts any channel shape: @handle, @handle/videos, /channel/UC..., /c/name,
/user/name, or a playlist URL. Routes through YOUTUBE_BIND_IP if set
(dump_channel.bat auto-detects the phone-hotspot adapter).

Deps: yt-dlp
"""

# ==========================================================================
DEFAULT_URL   = "https://www.youtube.com/@johnnyharris/videos"
INCLUDE_DURATION = True         # show [h:mm:ss] in each title comment
BLANK_BETWEEN = True            # blank line between entries (easier to scan)
# ==========================================================================

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def bind_ip() -> str:
    return os.getenv("YOUTUBE_BIND_IP", "").strip()


def fmt_hms(sec) -> str:
    if not sec:
        return "?:??"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def handle_from_url(url: str) -> str:
    m = re.search(r"@([A-Za-z0-9_.\-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/(?:channel|c|user)/([A-Za-z0-9_\-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]list=([A-Za-z0-9_\-]+)", url)
    if m:
        return "playlist_" + m.group(1)
    return "channel"


def flatten(info: dict):
    """Yield (id, title, duration) for every video entry, recursing channel tabs."""
    out, seen = [], set()

    def walk(node):
        if not isinstance(node, dict):
            return
        entries = node.get("entries")
        if entries is not None:
            for e in entries:
                walk(e)
            return
        vid = node.get("id") or ""
        if _VIDEO_ID_RE.match(vid) and vid not in seen:
            seen.add(vid)
            out.append((vid, (node.get("title") or "").strip(), node.get("duration")))

    walk(info)
    return out


def dump_one(url: str, ip: str) -> int:
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": "in_playlist", "ignore_no_formats_error": True,
    }
    if ip:
        opts["source_address"] = ip

    print(f"\nlisting: {url}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print("  ERROR:", str(e)[:200])
        return 1

    vids = flatten(info)
    if not vids:
        print("  no videos found (check the URL / that the channel has a videos tab).")
        return 1

    handle = handle_from_url(url)
    ch_name = info.get("channel") or info.get("uploader") or info.get("title") or handle
    out_path = BASE / f"channel_{_ILLEGAL.sub('', handle) or 'channel'}.txt"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines = [
        f"# {ch_name} - {len(vids)} videos - {stamp}",
        f"# source: {url}",
        "# Paste straight into links.txt (yt-grab skips these # comment lines),",
        "# or pick the ones you want. Newest first.",
        "",
    ]
    for vid, title, dur in vids:
        tag = f"[{fmt_hms(dur)}] " if INCLUDE_DURATION else ""
        lines.append(f"# {tag}{title}")
        lines.append(f"https://www.youtube.com/watch?v={vid}")
        if BLANK_BETWEEN:
            lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"  {ch_name}: {len(vids)} videos -> {out_path.name}")
    for vid, title, dur in vids[:5]:
        print(f"    [{fmt_hms(dur)}] {title[:70]}")
    if len(vids) > 5:
        print(f"    ... +{len(vids) - 5} more")
    return 0


def main() -> int:
    urls = sys.argv[1:] or [DEFAULT_URL]
    ip = bind_ip()
    print(f"egress: {'bound ' + ip if ip else 'default route'}")
    rc = 0
    for url in urls:
        rc |= dump_one(url, ip)
    return rc


if __name__ == "__main__":
    sys.exit(main())
