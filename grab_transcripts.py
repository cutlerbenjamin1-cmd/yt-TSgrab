#!/usr/bin/env python3
"""
yt-grab - gentle, pattern-resistant YouTube transcript harvester.

Reads YouTube URLs from links.txt, fetches each transcript, writes it to
transcripts/<Video Title>.txt, and records completed video IDs in _done.txt so
re-runs skip whatever is already grabbed.

Two independent safety layers:

  1. EGRESS (the load-bearing defense).  YouTube's caption (timedtext) endpoint
     is a SHARED per-IP rate limit - the thing that actually gets IPs banned.
     Define one or more egress SOURCES (EGRESS_SOURCES below); each binds the
     YouTube sockets to its own source IP (tether / resi / VPN) so they NEVER
     share a rate limit, and each runs its own independent timing + breaker.
     A source with an empty bind uses the machine default route.

  2. TIMING (hygiene).  A base cadence of REQUESTS_PER_HOUR, jittered so the gaps
     never form a detectable metronome.  The previous video's length seeds the
     jitter - mixed with live entropy (SEED_MODE="mixed") so two same-length
     videos don't produce an identical, spottable gap.

Deps: youtube-transcript-api, yt-dlp, httpx, requests, urllib3
"""

# ==========================================================================
#  CONFIG  -  turn these knobs
# ==========================================================================
# --- Egress sources -------------------------------------------------------
# Each source is one independent egress IP with its OWN rate budget, timing,
# and circuit breaker. They pull from a shared work queue (no video grabbed
# twice) but NEVER share a rate limit - YouTube's caption ban is per-IP, so
# each IP gets its own counter. Add a VPN / second tether by appending a line.
#   name     : label shown in the console  ([tether], [resi], ...)
#   bind     : egress source address. One of:
#                ""            -> machine default route
#                "1.2.3.4"     -> bind to this literal source IP
#                "auto:Wi-Fi"  -> auto-detect that adapter's current IPv4
#                "env:VAR"     -> read the IP from environment variable VAR
#   per_hour : this source's fetch cadence (its own budget)
#   enabled  : flip False to park a source without deleting it
#   jitter/min_gap/breaker : optional per-source overrides (else the defaults below)
EGRESS_SOURCES = [
    {"name": "tether", "bind": "auto:Wi-Fi", "per_hour": 35, "enabled": True},
    {"name": "resi",   "bind": "",           "per_hour": 35, "enabled": True},
    # {"name": "vpn",   "bind": "10.20.0.2",       "per_hour": 15, "enabled": False},
    # {"name": "phone2", "bind": "auto:Ethernet 2", "per_hour": 12, "enabled": False},
]

# --- Timing defaults (per source unless overridden in EGRESS_SOURCES) ------
JITTER              = 0.5          # +/- fraction on the base interval
MIN_GAP_SECONDS     = 60           # hard floor between a source's fetches
LONG_BREAK_CHANCE   = 0.06         # ~1 in 16: take a longer "stepped away" pause instead
LONG_BREAK_MULT     = (2.0, 4.0)   # that pause = base * uniform(*this)
SEED_MODE           = "mixed"      # "mixed" (length+entropy, recommended) | "length" (pure, reproducible/patterned)
ENFORCE_HOURLY_CAP  = True         # rolling-60min ceiling backstop; variance can't exceed the rate
BREAKER_LIMIT       = 3            # retire a source after this many CONSECUTIVE blocks (protects that IP)
DOWN_LIMIT          = 3            # after this many CONSECUTIVE network errors, re-probe egress; retire only if it's truly down

LANGUAGES           = ["en"]       # caption language preference order
# Abort a source if its probed egress matches one listed here (or is
# unverifiable). Add any IP you've confirmed YouTube has caption-banned so the
# run refuses to fetch through it instead of burning it further. Empty by
# default - fill it in once you actually have a burned IP (RFC 5737 doc IP
# shown only as an example of the shape):
#   FORBID_IPS = ["203.0.113.45"]
FORBID_IPS          = []

INPUT_FILE          = "links.txt"
OUTPUT_DIR          = "transcripts"
DONE_FILE           = "_done.txt"
AGE_FILE            = "_age_restricted.txt"   # age-gated videos land here (skipped, never retried)
LOG_FILE            = "_harvest.log"
INCLUDE_HEADER      = True         # write a small provenance header atop each .txt
MAX_VIDEOS          = 0            # 0 = no limit; else stop after N successful grabs this run

# Cookies: cookieless is proven safer on a clean egress. Only set these if you
# hit "Sign in to confirm you're not a bot" AND you're on a safe egress IP.
# (env YOUTUBE_COOKIES_BROWSER / YOUTUBE_COOKIES_PROFILE override.)
COOKIES_BROWSER     = ""           # e.g. "firefox"
COOKIES_PROFILE     = ""           # profile dir/name path
# ==========================================================================

import json
import logging
import os
import queue
import random
import re
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

BASE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

log = logging.getLogger("yt-grab")


# --------------------------------------------------------------------------
#  Errors used for control flow
# --------------------------------------------------------------------------
class BlockedError(Exception):
    """IP-level rate limit / bot-check - trips the circuit breaker."""


class NoCaptionsError(Exception):
    """Video has no usable captions - permanent skip."""


class TransientError(Exception):
    """Network / egress failure (tether dropped, timeout, connection refused,
    DNS). NOT the video's fault - never permanent-skip; requeue and retry."""


class AgeRestrictedError(Exception):
    """Video needs age confirmation - captions unreachable without an
    authenticated session. Permanent skip; also logged to its own file."""


# --------------------------------------------------------------------------
#  Egress binding
# --------------------------------------------------------------------------
def _sync_transport(ip: str):
    return httpx.HTTPTransport(local_address=ip) if ip else None


def bound_get(url: str, ip: str, params=None, timeout: float = 30.0) -> httpx.Response:
    with httpx.Client(transport=_sync_transport(ip), timeout=timeout,
                      headers={"User-Agent": UA}) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r


def egress_ip(ip: str) -> str:
    # Probe THROUGH the bind so the FORBID_IPS guard sees the real egress
    # (e.g. the tether IP), not the box default route.
    try:
        return bound_get("https://api.ipify.org", ip, timeout=10.0).text.strip()
    except Exception:
        return "unknown"


def adapter_ip(name: str) -> str:
    """Current IPv4 of a named Windows adapter (e.g. 'Wi-Fi'). '' if not found."""
    try:
        out = subprocess.check_output(
            ["netsh", "interface", "ip", "show", "address", name],
            text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    m = re.search(r"IP Address:\s*([0-9.]+)", out)
    return m.group(1).strip() if m else ""


def resolve_bind(spec: str) -> str:
    """Resolve a source's bind spec to a concrete source IP ('' = default route).
    Forms: '' | 'auto:<Adapter>' | 'env:<VAR>' | literal IP."""
    spec = (spec or "").strip()
    if not spec:
        return ""
    if spec.startswith("auto:"):
        return adapter_ip(spec[5:].strip())
    if spec.startswith("env:"):
        return os.getenv(spec[4:].strip(), "").strip()
    return spec


def slog(name: str, msg: str, *args):
    log.info("[%s] " + msg, name, *args)


# --------------------------------------------------------------------------
#  Video ID extraction
# --------------------------------------------------------------------------
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(url_or_id: str):
    if not url_or_id:
        return None
    s = url_or_id.strip()
    if _VIDEO_ID_RE.match(s):
        return s
    try:
        parsed = urlparse(s if "://" in s else f"https://{s}")
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "youtu.be":
        cand = parsed.path.lstrip("/").split("/")[0]
        return cand if _VIDEO_ID_RE.match(cand) else None
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path in ("/watch", "/watch/"):
            cand = (parse_qs(parsed.query).get("v") or [""])[0]
            return cand if _VIDEO_ID_RE.match(cand) else None
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            return parts[1] if _VIDEO_ID_RE.match(parts[1]) else None
    return None


# --------------------------------------------------------------------------
#  Error classification  (robust across lib versions - inspects name + message)
# --------------------------------------------------------------------------
def classify_error(exc: Exception) -> str:
    blob = (type(exc).__name__ + " " + str(exc)).lower()
    # Age gate FIRST: its message ("Sign in to confirm your age") collides with
    # the bot-check keyword "sign in to confirm" below, so it must win before it
    # or an age-restricted video is mis-flagged as an IP block (breaker trip +
    # endless requeue). This is a permanent skip, not a block.
    if any(k in blob for k in ("confirm your age", "inappropriate for some users",
                               "age restricted", "age-restricted", "agerestricted")):
        return "age_restricted"
    # Rate limit / bot-check FIRST (trips the breaker, never a skip).
    if any(k in blob for k in ("ipblocked", "requestblocked", "too many request",
                               "429", "sign in to confirm", "not a bot", "ratelimit",
                               "rate limit", "blocked")):
        return "blocked"
    # Network / egress failure (dead tether, timeout, DNS, refused, reset, bad
    # bind). Checked BEFORE the video-state buckets so a connectivity blip is
    # never mistaken for "no captions". These NEVER permanent-skip.
    if any(k in blob for k in ("timed out", "timeout", "connecttimeout", "readtimeout",
                               "connection", "connectionerror", "newconnectionerror",
                               "connection refused", "actively refused", "connection reset",
                               "connection aborted", "remotedisconnected", "remote end closed",
                               "max retries", "failed to establish", "getaddrinfo",
                               "name resolution", "nameresolutionerror", "temporarily",
                               "network is unreachable", "unreachable", "no route to host",
                               "requested address is not valid", "proxyerror", "ssl",
                               "unable to download", "urlopen error", "winerror", "oserror",
                               "10049", "10051", "10054", "10060", "10061", "10065", "11001")):
        return "transient"
    if any(k in blob for k in ("transcriptsdisabled", "notranscriptfound",
                               "no transcript", "notranslatable", "disabled",
                               "no element found", "no captions")):
        return "no_transcript"
    if any(k in blob for k in ("unavailable", "private", "removed",
                               "invalidvideoid", "unplayable")):
        return "unavailable"
    return "error"


# --------------------------------------------------------------------------
#  Captions - PRIMARY: youtube-transcript-api (source-bound)
# --------------------------------------------------------------------------
def _bound_requests_session(ip: str):
    import requests
    from requests.adapters import HTTPAdapter

    class _SrcAdapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            kw["source_address"] = (ip, 0)
            return super().init_poolmanager(*a, **kw)

        def proxy_manager_for(self, *a, **kw):
            kw["source_address"] = (ip, 0)
            return super().proxy_manager_for(*a, **kw)

    s = requests.Session()
    ad = _SrcAdapter()
    s.mount("http://", ad)
    s.mount("https://", ad)
    return s


def _ytt_api(ip: str):
    from youtube_transcript_api import YouTubeTranscriptApi
    if ip:
        return YouTubeTranscriptApi(http_client=_bound_requests_session(ip))
    return YouTubeTranscriptApi()


def fetch_primary(vid: str, ip: str):
    fetched = _ytt_api(ip).fetch(vid, languages=LANGUAGES)
    segs = [(float(s.start), float(s.duration), s.text) for s in fetched.snippets]
    return segs, bool(fetched.is_generated)


# --------------------------------------------------------------------------
#  Captions - FALLBACK: yt-dlp (source-bound) + json3 parse
# --------------------------------------------------------------------------
def _ytdlp_opts(ip: str):
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "extract_flat": False,
        # Authenticated sessions can be offered only SABR/PO-token formats this
        # build can't select ("Requested format is not available"); we only want
        # subs + metadata, so let the format step be a non-fatal miss.
        "ignore_no_formats_error": True,
        "writesubtitles": True, "writeautomaticsub": True,
        "subtitlesformat": "json3/vtt/best",
        "subtitleslangs": LANGUAGES or ["en"],
    }
    if ip:
        opts["source_address"] = ip
    cb = os.getenv("YOUTUBE_COOKIES_BROWSER", COOKIES_BROWSER).strip().lower()
    if cb:
        cp = os.getenv("YOUTUBE_COOKIES_PROFILE", COOKIES_PROFILE).strip()
        opts["cookiesfrombrowser"] = (cb, cp, None, None) if cp else (cb,)
    return opts


def _pick_sub_url(info: dict):
    wants = [l for l in LANGUAGES] + [l.split("-")[0] for l in LANGUAGES]
    for source, is_auto in ((info.get("subtitles") or {}, False),
                            (info.get("automatic_captions") or {}, True)):
        for lang in wants:
            variants = source.get(lang)
            if not variants:
                for k, v in source.items():
                    if k.split("-")[0] == lang.split("-")[0]:
                        variants = v
                        break
            if variants:
                for want_ext in ("json3", "srv3", "vtt"):
                    for var in variants:
                        if var.get("ext") == want_ext:
                            return var.get("url"), var.get("ext"), is_auto
                return variants[0].get("url"), variants[0].get("ext"), is_auto
    return None, None, False


def _parse_json3(data: dict):
    segs = []
    for ev in data.get("events") or []:
        text = "".join(s.get("utf8", "") for s in (ev.get("segs") or []))
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        start = (ev.get("tStartMs", 0) or 0) / 1000.0
        dur = (ev.get("dDurationMs", 0) or 0) / 1000.0
        # roll-up captions repeat the previous line then append; drop exact repeats
        if segs and text == segs[-1][2]:
            continue
        segs.append((start, dur, text))
    return segs


def fetch_fallback(vid: str, ip: str):
    """Returns dict{segments,title,duration,is_generated} or None. Raises BlockedError."""
    import yt_dlp
    from yt_dlp.utils import DownloadError
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        with yt_dlp.YoutubeDL(_ytdlp_opts(ip)) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except DownloadError as e:
        k = classify_error(e)
        if k == "blocked":
            raise BlockedError(str(e))
        if k == "age_restricted":
            raise AgeRestrictedError(str(e)[:140])
        return None
    sub_url, ext, is_auto = _pick_sub_url(info)
    if not sub_url or ext != "json3":
        # v1 only parses json3 (YouTube offers it for essentially every track)
        return None
    try:
        raw = bound_get(sub_url, ip, timeout=30.0).text
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise BlockedError("timedtext 429")
        return None
    segs = _parse_json3(json.loads(raw))
    if not segs:
        return None
    return {"segments": segs, "title": (info.get("title") or "").strip(),
            "channel": (info.get("channel") or info.get("uploader") or "").strip(),
            "duration": info.get("duration"), "is_generated": is_auto}


# --------------------------------------------------------------------------
#  Title + channel - oEmbed (light, different endpoint, rarely throttled)
# --------------------------------------------------------------------------
def fetch_meta(vid: str, ip: str):
    """(title, channel) from oEmbed - both in one light call. Either may be ''."""
    try:
        r = bound_get("https://www.youtube.com/oembed", ip,
                      params={"url": f"https://www.youtube.com/watch?v={vid}",
                              "format": "json"}, timeout=20.0)
        j = r.json()
        return (j.get("title") or "").strip(), (j.get("author_name") or "").strip()
    except Exception:
        return "", ""


# --------------------------------------------------------------------------
#  One video: captions (primary -> fallback) + title + length
# --------------------------------------------------------------------------
def fetch_one(vid: str, ip: str) -> dict:
    title = None
    channel = ""
    source = "ytt"
    try:
        segs, is_gen = fetch_primary(vid, ip)
    except Exception as e:
        kind = classify_error(e)
        if kind == "blocked":
            raise BlockedError(str(e))
        if kind == "transient":
            raise TransientError(f"{type(e).__name__}: {str(e)[:140]}")
        if kind == "age_restricted":
            raise AgeRestrictedError(str(e)[:140])
        # Not blocked/transient: try the heavier yt-dlp path (also gives
        # title+channel). Guard it so a connectivity failure THERE is reported
        # as transient (retry), not as "no captions" (permanent skip).
        try:
            fb = fetch_fallback(vid, ip)   # may raise BlockedError / AgeRestrictedError
        except (BlockedError, AgeRestrictedError):
            raise
        except Exception as fe:
            raise TransientError(f"{type(fe).__name__}: {str(fe)[:140]}")
        if not fb:
            if kind in ("no_transcript", "unavailable"):
                raise NoCaptionsError(kind)
            # unclassified ("error"): don't gamble a permanent skip - retry later
            raise TransientError(f"{type(e).__name__}: {str(e)[:140]}")
        segs, is_gen = fb["segments"], fb.get("is_generated", False)
        title, source = fb.get("title") or None, "yt-dlp"
        channel = fb.get("channel") or ""
    if not segs:
        raise NoCaptionsError("empty")
    length = segs[-1][0] + segs[-1][1]     # caption span ~= video length; free entropy
    # oEmbed hands us BOTH title and channel in one light call
    if title is None or not channel:
        o_title, o_channel = fetch_meta(vid, ip)
        title = title if title is not None else (o_title or vid)
        channel = channel or o_channel
    return {"segments": segs, "title": title, "channel": channel or "Unknown Channel",
            "length": length, "is_generated": is_gen, "source": source}


# --------------------------------------------------------------------------
#  Output: title-safe filename + .txt writer
# --------------------------------------------------------------------------
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(title: str, fallback: str) -> str:
    name = _ILLEGAL.sub("", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if not name or name.split(".")[0].upper() in _RESERVED:
        name = f"{name} {fallback}".strip()
    if len(name) > 150:
        name = name[:150].rstrip(". ")
    return name or fallback


def unique_path(out_dir: Path, base_name: str, vid: str) -> Path:
    p = out_dir / f"{base_name}.txt"
    if not p.exists():
        return p
    p = out_dir / f"{base_name} [{vid}].txt"     # different video, same title
    if not p.exists():
        return p
    i = 2
    while (out_dir / f"{base_name} [{vid}]_{i}.txt").exists():
        i += 1
    return out_dir / f"{base_name} [{vid}]_{i}.txt"


def fmt_hms(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def reflow(segments) -> str:
    raw = " ".join(t for (_s, _d, t) in segments)
    raw = re.sub(r"\s+", " ", raw).strip()
    return textwrap.fill(raw, width=100) if raw else ""


def write_transcript(out_dir: Path, vid: str, res: dict) -> Path:
    channel = res.get("channel") or "Unknown Channel"
    channel_dir = out_dir / sanitize_filename(channel, "Unknown Channel")
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(channel_dir, sanitize_filename(res["title"], vid), vid)
    parts = []
    if INCLUDE_HEADER:
        parts += [
            f"Title: {res['title']}",
            f"Channel: {channel}",
            f"URL: https://www.youtube.com/watch?v={vid}",
            f"Duration: {fmt_hms(res['length'])}",
            f"Captions: {'auto-generated' if res['is_generated'] else 'manual'} (via {res['source']})",
            f"Fetched: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
        ]
    parts.append(reflow(res["segments"]))
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
#  Input / tracking
# --------------------------------------------------------------------------
def load_links(path: Path):
    out, seen = [], set()
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        vid = extract_video_id(s)
        if vid and vid not in seen:
            seen.add(vid)
            out.append((s, vid))
    return out


def load_done(path: Path) -> set:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.split("\t")[0].split()[0])
    return out


def mark_done(path: Path, vid: str, note: str):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{vid}\t{note}\t{ts}\n")


def record_age_restricted(age_path: Path, url: str, vid: str):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = not age_path.exists()
    with age_path.open("a", encoding="utf-8") as f:
        if new:
            f.write("# yt-grab: videos skipped - they require age confirmation "
                    "(captions unreachable without an authed session).\n"
                    "# Comment lines are ignored by links.txt; the URLs are paste-ready.\n\n")
        f.write(f"# age-restricted  {ts}  (from: {url})\n")
        f.write(f"https://www.youtube.com/watch?v={vid}\n\n")


def load_age_ids(path: Path) -> set:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        v = extract_video_id(s)
        if v:
            out.add(v)
    return out


# --------------------------------------------------------------------------
#  Timing engine
# --------------------------------------------------------------------------
def compute_delay(prev_length, src) -> tuple:
    """Return (delay_seconds, is_long_break) for ONE source using its own
    per_hour / jitter / min_gap. Jitter is seeded per SEED_MODE."""
    base = 3600.0 / max(src.per_hour, 1e-9)
    length_component = int((prev_length or 0) * 1000)
    if SEED_MODE == "length":
        seed = length_component
    else:  # mixed: length is folded in, but os.urandom guarantees it never
        # repeats (time.time_ns() is ~15ms-coarse on Windows and would collide
        # on back-to-back calls, silently recreating a fixed cadence).
        seed = (length_component * 2654435761) ^ int.from_bytes(os.urandom(8), "little")
    rng = random.Random(seed)
    long_break = rng.random() < LONG_BREAK_CHANCE
    if long_break:
        delay = base * rng.uniform(*LONG_BREAK_MULT)
    else:
        delay = base * rng.uniform(1.0 - src.jitter, 1.0 + src.jitter)
    return max(src.min_gap, delay), long_break


class Source:
    """One independent egress: its own IP, rate, timing, breaker, stats."""
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.bind_spec = cfg.get("bind", "")
        self.per_hour = cfg.get("per_hour", 10)
        self.enabled = cfg.get("enabled", True)
        self.jitter = cfg.get("jitter", JITTER)
        self.min_gap = cfg.get("min_gap", MIN_GAP_SECONDS)
        self.breaker_limit = cfg.get("breaker", BREAKER_LIMIT)
        self.down_limit = cfg.get("down", DOWN_LIMIT)
        self.bind_ip = ""       # resolved at startup
        self.egress = "?"       # probed egress IP
        self.active = False
        self.grabbed = 0
        self.skipped = 0
        self.blocks = 0
        self.transient = 0


class HourlyCap:
    """Rolling-60min ceiling for ONE source (never shared across sources)."""
    def __init__(self, src: "Source"):
        self.src = src
        self.stamps = deque()

    def gate(self):
        if not ENFORCE_HOURLY_CAP or self.src.per_hour <= 0:
            return
        now = time.time()
        while self.stamps and now - self.stamps[0] > 3600:
            self.stamps.popleft()
        if len(self.stamps) >= self.src.per_hour:
            wait = 3600 - (now - self.stamps[0]) + 1
            if wait > 0:
                slog(self.src.name, "[cap] hourly ceiling (%d/hr) reached, waiting %s",
                     self.src.per_hour, fmt_hms(wait))
                interruptible_sleep(wait)

    def record(self):
        self.stamps.append(time.time())


# --------------------------------------------------------------------------
#  Stop control + interruptible sleep
# --------------------------------------------------------------------------
def stop_requested() -> bool:
    return (BASE / "_STOP").exists()


def interruptible_sleep(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        if stop_requested():
            raise KeyboardInterrupt("_STOP file present")
        time.sleep(min(2.0, max(0.0, end - time.time())))


# --------------------------------------------------------------------------
#  Logging
# --------------------------------------------------------------------------
def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(BASE / LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.handlers[:] = [sh, fh]


# --------------------------------------------------------------------------
#  Main
# --------------------------------------------------------------------------
def _resolve_sources():
    """Build Source objects, probe each egress independently, return the active ones.
    A dead/forbidden/duplicate egress disables just THAT source, never the run."""
    sources = [Source(c) for c in EGRESS_SOURCES]
    active = []
    for s in sources:
        if not s.enabled:
            slog(s.name, "disabled (config) - skipping")
            continue
        s.bind_ip = resolve_bind(s.bind_spec)
        if s.bind_spec and not s.bind_ip:
            slog(s.name, "DISABLED: bind '%s' resolved to nothing (adapter down / env unset)",
                 s.bind_spec)
            continue
        s.egress = egress_ip(s.bind_ip)
        where = f"bind {s.bind_ip}" if s.bind_ip else "default route"
        slog(s.name, "egress %s  (%s)  cadence %g/hr", s.egress, where, s.per_hour)
        if FORBID_IPS and (s.egress in FORBID_IPS or s.egress == "unknown"):
            slog(s.name, "DISABLED: egress forbidden or unverifiable (FORBID_IPS guard)")
            continue
        s.active = True
        active.append(s)

    # Two active sources on the SAME egress IP would share the per-IP rate limit
    # - the exact thing this design exists to avoid. Warn loudly (don't kill).
    seen = {}
    for s in active:
        if s.egress in seen:
            slog(s.name, "WARNING: same egress %s as [%s] - they SHARE a rate limit!",
                 s.egress, seen[s.egress])
        else:
            seen[s.egress] = s.name
    return active


def _probe_down(src):
    """Re-probe a source's egress after repeated network errors. Returns
    (is_down, probe_ip). Self-heals: if the adapter came back with a new lease
    (hotspot reconnected on a different IP), adopt it and report NOT down."""
    new_bind = resolve_bind(src.bind_spec)
    probe_ip = new_bind if new_bind else src.bind_ip
    eg = egress_ip(probe_ip)
    down = (eg == "unknown") or bool(FORBID_IPS and eg in FORBID_IPS) or bool(src.bind_spec and not new_bind)
    if not down:
        if new_bind and new_bind != src.bind_ip:
            slog(src.name, "egress recovered on new bind %s (was %s)", new_bind, src.bind_ip or "default")
            src.bind_ip = new_bind
        src.egress = eg
    return down, eg


def _run_source(src, q, out_dir, done_path, done_lock, stats, stat_lock, age_path, age_seen):
    """One source's worker loop: claim from the shared queue, fetch on its own
    bound egress, on its own cadence, with its own breaker + down-detection.
    A network failure (dead tether) is NEVER a permanent skip - the video is
    requeued; only a confirmed-down egress retires the whole source."""
    cap = HourlyCap(src)
    breaker = 0
    consec_net = 0
    prev_length = None

    def pace():
        delay, long_break = compute_delay(prev_length, src)
        slog(src.name, "next in %s%s", fmt_hms(delay), "  [long break]" if long_break else "")
        interruptible_sleep(delay)

    def handle_net(url, vid, label, detail):
        """TransientError / unexpected error: requeue (never skip), and retire
        only if a re-probe confirms the egress is actually down. Returns True
        to retire the source."""
        nonlocal consec_net
        consec_net += 1
        src.transient += 1
        slog(src.name, "%s (%d/%d): %s - requeued, NOT skipping", label,
             consec_net, src.down_limit, detail[:140])
        q.put((url, vid))
        if consec_net >= src.down_limit:
            down, eg = _probe_down(src)
            if down:
                slog(src.name, "egress DOWN (probe=%s) after %d network errors - RETIRING "
                     "source; its videos stay queued for healthy sources.", eg, consec_net)
                return True
            slog(src.name, "egress still up (%s) - backing off, continuing.", eg)
            consec_net = 0
        interruptible_sleep(min(30.0, 3600.0 / max(src.per_hour, 1e-9)))
        return False

    try:
        while not stop_requested():
            try:
                url, vid = q.get_nowait()
            except queue.Empty:
                slog(src.name, "queue drained - source done (grabbed %d)", src.grabbed)
                return

            cap.gate()
            slog(src.name, "fetch %s  (~%d left)", vid, q.qsize())
            try:
                res = fetch_one(vid, src.bind_ip)
                cap.record()
            except BlockedError as e:
                cap.record()
                breaker += 1
                src.blocks += 1
                slog(src.name, "BLOCKED (%d/%d): %s", breaker, src.breaker_limit, str(e)[:150])
                q.put((url, vid))          # hand it back for a healthy source to retry
                if breaker >= src.breaker_limit:
                    slog(src.name, "circuit breaker tripped - RETIRING this source to protect its IP.")
                    return
                pace()
                continue
            except TransientError as e:
                if handle_net(url, vid, "NET-ERR", str(e)):
                    return
                continue
            except NoCaptionsError as e:
                with done_lock:
                    mark_done(done_path, vid, f"skip:{e}")
                with stat_lock:
                    stats["skipped"] += 1
                src.skipped += 1
                breaker = 0
                consec_net = 0
                slog(src.name, "no captions (%s) - skip permanently", e)
                pace()
                continue
            except AgeRestrictedError:
                with done_lock:
                    mark_done(done_path, vid, "skip:age_restricted")
                    if vid not in age_seen:
                        age_seen.add(vid)
                        record_age_restricted(age_path, url, vid)
                with stat_lock:
                    stats["age_restricted"] += 1
                src.skipped += 1
                breaker = 0
                consec_net = 0
                slog(src.name, "age-restricted - skip permanently (logged to %s)", age_path.name)
                pace()
                continue
            except Exception as e:
                # anything unclassified - treat as network/transient, never skip
                if handle_net(url, vid, "ERR", f"{type(e).__name__}: {e}"):
                    return
                continue

            breaker = 0
            consec_net = 0
            prev_length = res["length"]
            path = write_transcript(out_dir, vid, res)
            with done_lock:
                mark_done(done_path, vid, "ok")
            src.grabbed += 1
            with stat_lock:
                stats["grabbed"] += 1
                total = stats["grabbed"]
            slog(src.name, "OK  %s  (%s, %d segs, %s) -> %s",
                 fmt_hms(res["length"]), res["source"], len(res["segments"]),
                 "auto" if res["is_generated"] else "manual", path.relative_to(out_dir))
            if MAX_VIDEOS and total >= MAX_VIDEOS:
                slog(src.name, "MAX_VIDEOS (%d) reached - retiring.", MAX_VIDEOS)
                return
            pace()
    except KeyboardInterrupt:
        slog(src.name, "stop signalled - retiring.")
        return


def main() -> int:
    setup_logging()
    out_dir = BASE / OUTPUT_DIR
    out_dir.mkdir(exist_ok=True)
    done_path = BASE / DONE_FILE

    log.info("=" * 68)
    log.info("yt-grab (multi-source) start")

    active = _resolve_sources()
    if not active:
        log.error("no active egress sources - aborting. Check tether/VPN/FORBID_IPS.")
        return 2

    links = load_links(BASE / INPUT_FILE)
    done = load_done(done_path)
    pending = [(u, v) for (u, v) in links if v not in done]
    log.info("links: %d unique | done: %d | pending: %d | sources: %s",
             len(links), len(done), len(pending),
             ", ".join(f"{s.name}@{s.per_hour}/hr" for s in active))
    if not pending:
        log.info("nothing to do.")
        return 0
    total_rate = sum(s.per_hour for s in active)
    log.info("aggregate ~%g/hr across %d source(s) | seed=%s | est >= %s",
             total_rate, len(active), SEED_MODE,
             fmt_hms(len(pending) / max(total_rate, 1e-9) * 3600))

    q = queue.Queue()
    for item in pending:
        q.put(item)

    done_lock = threading.Lock()
    stat_lock = threading.Lock()
    stats = {"grabbed": 0, "skipped": 0, "age_restricted": 0}
    age_path = BASE / AGE_FILE
    age_seen = load_age_ids(age_path)

    threads = []
    for s in active:
        t = threading.Thread(target=_run_source, name=s.name, daemon=True,
                             args=(s, q, out_dir, done_path, done_lock, stats,
                                   stat_lock, age_path, age_seen))
        t.start()
        threads.append(t)

    stopped = None
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
            if stop_requested():
                stopped = "_STOP"
                break
    except KeyboardInterrupt:
        stopped = "interrupted"
        (BASE / "_STOP").touch()
    if stopped:
        log.info("stopping (%s) - waiting for workers to finish current fetch...", stopped)
        for t in threads:
            t.join(timeout=45.0)

    log.info("-" * 68)
    for s in active:
        log.info("[%s] grabbed %d | skipped %d | blocks %d | net-errors %d",
                 s.name, s.grabbed, s.skipped, s.blocks, s.transient)
    log.info("TOTAL grabbed %d | skipped %d | age-restricted %d | remaining ~%d%s",
             stats["grabbed"], stats["skipped"], stats["age_restricted"], q.qsize(),
             f" | stopped: {stopped}" if stopped else "")

    stop_file = BASE / "_STOP"
    if stop_file.exists():
        try:
            stop_file.unlink()      # consume it so the next run isn't instantly aborted
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
