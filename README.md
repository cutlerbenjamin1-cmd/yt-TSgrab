# yt-TSgrab

A careful YouTube transcript grabber. Give it a list of YouTube URLs and it
pulls each video's captions, writing clean `.txt` transcripts sorted into
per-channel folders. The whole point is to do this in bulk without getting the
source IP caption-banned.

The awkward part of pulling captions at volume is not the parsing. It's that
YouTube's caption (`timedtext`) endpoint has a rate limit that is shared and
tied to your IP. Go too fast and that IP stops serving captions, in the browser
as well as to a script, and it can stay that way for a while. Everything here is
built around staying under that limit.

---

## What it does

- Pulls captions for the videos in `links.txt`. It tries `youtube-transcript-api`
  first and falls back to `yt-dlp` with a `json3` parser.
- Writes one `.txt` per video to `transcripts/<Channel>/<Title>.txt`, with a
  short header and the caption text reflowed to 100 columns.
- Records finished video IDs in `_done.txt`, so a re-run skips whatever it
  already has. Keep appending links and re-running; it only fetches the new ones.
- Skips age-restricted videos instead of retrying them (their captions need a
  signed-in, age-confirmed session) and lists each one in `_age_restricted.txt`,
  a paste-ready record you can revisit later.
- Splits the work across one or more egress sources (a phone tether, a
  residential line, a VPN), each with its own IP, hourly budget, timing and
  circuit breaker, so no two of them share a rate limit.
- `dump_channel.py` dumps every video on a channel to a paste-ready list.

## Requirements

- Python 3.9+
- `pip install -r requirements.txt`
  (`youtube-transcript-api`, `yt-dlp`, `httpx`, `requests`, `urllib3`)
- The Python runs anywhere. The `.bat` launchers and the `auto:<Adapter>` IP
  detection use Windows `netsh`; on Linux or macOS, bind by literal IP or
  `env:VAR` and run `python grab_transcripts.py` directly.

## Quick start

1. Put YouTube URLs in `links.txt`. Any shape works (`watch?v=`, `youtu.be/`,
   `shorts/`, `live/`, `embed/`, or a bare 11-character ID), and `#` lines are
   ignored.
2. Open `grab_transcripts.py` and set `EGRESS_SOURCES` (below).
3. Run `run.bat`, or `python grab_transcripts.py`.

Transcripts land in `transcripts/<Channel>/`. To stop, hit Ctrl+C or drop an
empty file named `_STOP` in the folder; the workers finish their current fetch
and exit, and the `_STOP` file is cleared on the next start.

### List a whole channel

```
dump_channel.bat https://www.youtube.com/@SomeChannel/videos
```

This writes `channel_<handle>.txt`, ready to paste into `links.txt`. It reads the
channel's *browse* endpoint, not the caption endpoint, so it's cheap and spends
none of your caption budget. It takes any channel form (`@handle`,
`/channel/UC...`, `/c/name`, `/user/name`) or a playlist URL.

---

## Configuration

The knobs are all at the top of `grab_transcripts.py`.

### Egress sources

```python
EGRESS_SOURCES = [
    {"name": "tether", "bind": "auto:Wi-Fi", "per_hour": 35, "enabled": True},
    {"name": "resi",   "bind": "",           "per_hour": 35, "enabled": True},
]
```

Each entry is one egress IP with its own rate budget, timing and breaker. They
share a work queue, so nothing is fetched twice, but they never share a rate
limit; the caption ban is per IP, so each IP keeps its own counter. `bind` takes
one of:

| `bind` value   | meaning                                                 |
|----------------|---------------------------------------------------------|
| `""`           | machine default route                                   |
| `"1.2.3.4"`    | bind sockets to this literal source IP                  |
| `"auto:Wi-Fi"` | auto-detect that adapter's *current* IPv4 each launch   |
| `"env:VAR"`    | read the IP from environment variable `VAR`             |

Add a line for a VPN or a second tether; set `"enabled": False` to shelve one
without deleting it. Per-source overrides you can add: `jitter`, `min_gap`,
`breaker`, `down`.

### Timing and safety knobs

| Setting              | Default         | What it does                                                       |
|----------------------|-----------------|--------------------------------------------------------------------|
| `per_hour`           | 35 (per source) | Fetch cadence for that source. Total = sum across sources.          |
| `JITTER`             | 0.5             | +/- fraction on the base interval, so gaps aren't a metronome.      |
| `MIN_GAP_SECONDS`    | 60              | Hard floor between a source's fetches.                              |
| `LONG_BREAK_CHANCE`  | 0.06            | About 1 in 16 fetches, take a longer "stepped away" pause instead.  |
| `LONG_BREAK_MULT`    | (2.0, 4.0)      | That pause = base x uniform(2, 4).                                  |
| `SEED_MODE`          | `"mixed"`       | `mixed` seeds jitter from length plus live entropy; `length` is pure and reproducible (and therefore detectable). Use `mixed`. |
| `ENFORCE_HOURLY_CAP` | True            | Rolling 60-minute ceiling; jitter can never push a source over `per_hour`. |
| `BREAKER_LIMIT`      | 3               | Retire a source after this many *consecutive* blocks, to protect its IP. |
| `DOWN_LIMIT`         | 3               | After this many *consecutive* network errors, re-probe egress; retire only if it's really down. |
| `FORBID_IPS`         | `[]`            | Egress IPs to refuse (e.g. one you've confirmed is caption-banned). |
| `LANGUAGES`          | `["en"]`        | Caption language preference order.                                 |
| `MAX_VIDEOS`         | 0               | 0 = no limit; otherwise stop after N successful grabs this run.     |

### Output format

Each transcript is a UTF-8 `.txt`:

```
Title: <video title>
Channel: <channel name>
URL: https://www.youtube.com/watch?v=<id>
Duration: H:MM:SS
Captions: auto-generated|manual (via ytt|yt-dlp)
CaptionType: auto|manual
Fetched: <ISO-8601 UTC>

<caption text, punctuation as YouTube provides it, reflowed to 100 cols>
```

Set `INCLUDE_HEADER = False` to write just the text.

---

## Advice and gotchas

The stuff that actually mattered:

- The `timedtext` caption endpoint is the whole problem. Its rate limit is per
  egress IP and shared, so when you trip it captions stop rendering in the
  browser on that IP too, not just in your script. Slower is just safer; no
  header trick buys you more throughput.

- Add IPs, don't crank the rate. Two different egress IPs at 35/hr each is far
  safer than one IP at 70/hr. Two sources that land on the *same* IP silently
  share the limit, so the tool probes each egress at startup and warns loudly if
  two collide.

- Cookieless is safer on a clean IP. Only reach for cookies if you actually hit
  "Sign in to confirm you're not a bot", and even then only on a safe egress.

- Once an IP is caption-banned, treat it as gone; ours never came back clean.
  Start a new IP low (10-20/hr), confirm it's clean, then ramp up to ~35/hr.

- Network hiccups (dead tether, timeout, DNS) are never "no captions"; those get
  requeued, and only real block/ban states count against a source. Hotspot IPs
  also change on reconnect, so `auto:Wi-Fi` re-reads the live address each launch.

- Keep `_done.txt`. It's the resume file that makes re-runs skip whatever's
  already grabbed.

---

## Files

| File                  | Purpose                                                    |
|-----------------------|------------------------------------------------------------|
| `grab_transcripts.py` | The grabber. All config lives at the top.                  |
| `run.bat`             | Windows launcher for the grabber.                          |
| `dump_channel.py`     | List every video on a channel to `channel_<handle>.txt`.   |
| `dump_channel.bat`    | Windows launcher; auto-binds the Wi-Fi/hotspot egress.     |
| `migrate_channels.py` | One-off backfill of old flat transcripts into subfolders.  |
| `links.txt`           | Your input list (an example is included).                  |
| `requirements.txt`    | Python dependencies.                                       |

Runtime output (`transcripts/`, `_done.txt`, `_age_restricted.txt`,
`_harvest.log`, `channel_*.txt`) is git-ignored.

## Responsible use

This is meant for personal and research use. Respect YouTube's Terms of Service,
keep the request rate gentle, and don't repost creators' work in ways their
licenses don't allow. Keeping the rate low is partly about not getting banned and
partly just about not being a nuisance.

## License

MIT. See [LICENSE](LICENSE). Use it however you want.
