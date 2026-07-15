#!/usr/bin/env python3
"""
repair_captions.py - batch repair of YouTube auto-captions with the bench winner
Qwen3.6-35B-A3B-MTP. Reads transcripts/**/*.txt (CaptionType:auto only) from the
repo root, writes cleaned copies to repaired/transcripts/**, and keeps a resumeable
JSONL manifest (keyed on video_id; manual files indexed as passthrough) that a
later step uses to merge repaired-auto with the gold manual captions.

Two lanes (only one 22.7GB model fits the 24GB GPU at a time):
  small : ctx 16384, --n-cpu-moe 2  -> words <= 6000 (short <=2800w may run 2-up)
  large : ctx 32768, --n-cpu-moe 4  -> 6000 < words <= 11000 one-shot; >11000 chunked

Leaves transcripts/ untouched; safe to run alongside the scrape (read-only on the
source tree; new files scraped mid-run are picked up on the next run).

Usage:
  python repair_captions.py --dry-run             # plan only: no GPU, no writes
  python repair_captions.py --mode index          # build manifest map (manual+done), no GPU
  python repair_captions.py --mode small          # launch small server -> short+medium
  python repair_captions.py --mode large          # launch large server -> large+huge
  python repair_captions.py --mode both           # small lane then large lane
Options:
  --limit N            cap files PER BUCKET (smoke tests)
  --channel NAME       restrict to one channel folder
  --small-concurrency  1 (default) or 2 (fire 2 short jobs at once)
  --halluc-check       re-run each file at seed 43, flag seed-divergent proper nouns
  --reverse            process largest-first within each group
"""
import argparse
import csv
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------
#  Paths / config
# --------------------------------------------------------------------------
BASE         = Path(__file__).resolve().parent          # repair/
REPO_ROOT    = BASE.parent                              # yt-TSgrab/  (scraper repo root)
SRC_DIR      = REPO_ROOT / "transcripts"                # grab_transcripts.py output
OUT_ROOT     = REPO_ROOT / "repaired"                   # repair output (gitignored)
OUT_TX       = OUT_ROOT / "transcripts"
MANIFEST     = OUT_ROOT / "manifest.jsonl"
REVIEW       = OUT_ROOT / "review_flags.jsonl"
OVERSIZED_REVIEW = OUT_ROOT / "oversized_review.jsonl"
RUNLOG       = OUT_ROOT / "repair.log"
EXCLUDE_DIRS = {"Blueprint"}                            # separate .md harvest, no caption flag

PROMPT_PATH  = BASE / "prompt.txt"                      # bundled system prompt (v1 conservative)

# --- EDIT THESE two paths for your machine -------------------------------
#   SERVER_BIN : a llama-server.exe built with MTP support (--spec-type draft-mtp)
#   MODEL      : the Qwen3.6-35B-A3B-MTP UD-Q4_K_M GGUF (or your own model)
# See README.md -> Configuration for changing the model, lanes, or sampler.
SERVER_BIN   = r"G:\LM\llama-cpp-src\build\bin\llama-server.exe"
MODEL        = r"G:/caption-repair-bench/models/Qwen3.6-35B-A3B-MTP/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
# -------------------------------------------------------------------------
MODEL_ID     = "qwen3.6-35b-a3b-mtp"
PROMPT_VER   = "v1_conservative"

SAMPLER    = {"temperature": 0.2, "top_p": 0.8, "top_k": 20, "min_p": 0.0}
SEED       = 42
ALT_SEED   = 43
REQ_EXTRA  = {"chat_template_kwargs": {"enable_thinking": False}}

# bucket thresholds (body word count)
SHORT_MAX    = 2800    # small lane, parallel-eligible (2 fit a 16384 unified-KV pool)
SMALL_MAX    = 6000    # small lane upper bound (one-shot at ctx 16384)
HUGE_MIN     = 11000   # above this -> chunk on the large lane
OVERSIZED_MAX = 80000  # above this -> SKIP + flag to oversized_review.jsonl (compilations/livestreams)
CHUNK_WORDS  = 6000    # target words per chunk when chunking
TOK_PER_WORD = 2.6     # round-trip token estimate (prompt+completion), from bench

LANES = {
    "small": dict(port=8181, ctx=16384, cpu_moe=2, tps=155),
    "large": dict(port=8182, ctx=32768, cpu_moe=4, tps=139),
}
COMMON_ARGS = ["-ngl", "99", "--jinja", "-fa", "on",
               "--spec-type", "draft-mtp", "--spec-draft-n-max", "2",
               "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]

LANE_OF = {"short": "small", "medium": "small", "large": "large", "huge": "large"}

PROMPT = PROMPT_PATH.read_text(encoding="utf-8").strip()

_manifest_lock = threading.Lock()
_review_lock   = threading.Lock()
_log_lock      = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(*a):
    line = f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(x) for x in a)
    with _log_lock:
        print(line, flush=True)
        try:
            with open(RUNLOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# --------------------------------------------------------------------------
#  Header parse / bucketing / hashing
# --------------------------------------------------------------------------
HDR_SPLIT = re.compile(r"\r?\n\r?\n")
VID_RE    = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def _field(name, blob):
    m = re.search(rf"^{name}:\s*(.+)$", blob, re.MULTILINE)
    return m.group(1).strip() if m else ""


def parse_file(path):
    """Return (meta_dict, body). meta = title, url, video_id, caption_type."""
    txt = path.read_text(encoding="utf-8", errors="replace")
    parts = HDR_SPLIT.split(txt, maxsplit=1)
    header = parts[0]
    body = parts[1] if len(parts) == 2 else ""
    url = _field("URL", header)
    m = VID_RE.search(url)
    vid = m.group(1) if m else "path:" + hashlib.sha1(str(path).encode()).hexdigest()[:11]
    meta = dict(title=_field("Title", header) or path.stem,
                url=url,
                video_id=vid,
                caption_type=_field("CaptionType", header).lower())
    return meta, body


def word_count(s):
    return len(s.split())


def bucket_of(wc):
    if wc <= SHORT_MAX:
        return "short"
    if wc <= SMALL_MAX:
        return "medium"
    if wc <= HUGE_MIN:
        return "large"
    return "huge"


def body_sha(body):
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------
#  Faithfulness signals
# --------------------------------------------------------------------------
_PUNCT = re.compile(r"[^\w\s]")


def _norm_words(s):
    return _PUNCT.sub(" ", s.lower()).split()


def norm_sim(src, out):
    """Punctuation-insensitive word-sequence similarity (0..1)."""
    a, b = _norm_words(src), _norm_words(out)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def len_ratio(src, out):
    return word_count(out) / max(1, word_count(src))


def out_budget(words, ctx):
    """Completion-token budget: room for the repaired output (~1.3 tok/word) with
    headroom, capped so prompt+completion stays inside ctx. Fixes truncation of
    long one-shot files where a static max_tokens would cut the output short."""
    return max(1024, min(ctx - int(1.3 * words) - 700, int(1.6 * words) + 512))


TITLE_RE = re.compile(r"\b[A-Z][A-Za-z'’-]{2,}\b")


def proper_nouns(s):
    return Counter(TITLE_RE.findall(s))


# --------------------------------------------------------------------------
#  Chunking (huge transcripts) - cut on existing line boundaries, no overlap
# --------------------------------------------------------------------------
def chunk_body(body, target=CHUNK_WORDS):
    lines = body.splitlines()
    chunks, cur, cw = [], [], 0
    for ln in lines:
        w = len(ln.split())
        if cw + w > target and cur:
            chunks.append("\n".join(cur))
            cur, cw = [], 0
        cur.append(ln)
        cw += w
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [body]


# --------------------------------------------------------------------------
#  Enumeration
# --------------------------------------------------------------------------
def iter_sources(channel=None):
    for path in sorted(SRC_DIR.rglob("*.txt")):
        rel = path.relative_to(SRC_DIR)
        if rel.parts and rel.parts[0] in EXCLUDE_DIRS:
            continue
        if channel and (not rel.parts or rel.parts[0] != channel):
            continue
        try:
            meta, body = parse_file(path)
        except Exception as e:
            log("PARSE-FAIL", rel, repr(e))
            continue
        yield path, rel, meta, body


# --------------------------------------------------------------------------
#  Manifest (the merge artifact)
# --------------------------------------------------------------------------
def load_manifest():
    idx = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            idx[rec.get("video_id")] = rec          # last record wins
    return idx


def append_manifest(rec):
    with _manifest_lock:
        with open(MANIFEST, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_review(rec):
    with _review_lock:
        with open(REVIEW, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_oversized(rec):
    with _review_lock:
        with open(OVERSIZED_REVIEW, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def is_done(idx, meta, body):
    rec = idx.get(meta["video_id"])
    return bool(rec and rec.get("status") == "done"
                and rec.get("source_sha256") == body_sha(body))


def manual_record(rel, meta, body):
    return dict(video_id=meta["video_id"], channel=rel.parts[0], title=meta["title"],
                source_path="transcripts/" + rel.as_posix(), caption_type="manual",
                word_count=word_count(body), bucket="n/a", status="skipped_manual",
                output_path=None, model=None, prompt_version=None, seed=None,
                len_ratio=None, norm_sim=None, chunks=0, flags=[], gen_tps=None,
                completion_tokens=0, wall_s=0, repaired_at=now_iso(),
                source_sha256=body_sha(body), error=None)


def index_manual(manuals, idx):
    n = 0
    for path, rel, meta, body in manuals:
        prev = idx.get(meta["video_id"])
        if prev and prev.get("status") == "skipped_manual":
            continue
        rec = manual_record(rel, meta, body)
        append_manifest(rec)
        idx[meta["video_id"]] = rec
        n += 1
    if n:
        log(f"indexed {n} manual passthrough records")


def oversized_record(rel, meta, body):
    return dict(video_id=meta["video_id"], channel=rel.parts[0], title=meta["title"],
                source_path="transcripts/" + rel.as_posix(), caption_type="auto",
                word_count=word_count(body), bucket="oversized", status="skipped_oversized",
                output_path=None, model=None, prompt_version=None, seed=None,
                len_ratio=None, norm_sim=None, chunks=0, flags=["TO_REVIEW"], gen_tps=None,
                completion_tokens=0, wall_s=0, repaired_at=now_iso(),
                source_sha256=body_sha(body), error=None)


def index_oversized(oversized, idx):
    n = 0
    for rel, meta, body in oversized:
        prev = idx.get(meta["video_id"])
        if prev and prev.get("status") == "skipped_oversized":
            continue
        rec = oversized_record(rel, meta, body)
        append_manifest(rec)
        append_oversized(dict(video_id=meta["video_id"], channel=rel.parts[0], title=meta["title"],
                              word_count=word_count(body), source_path="transcripts/" + rel.as_posix(),
                              reason=f"over {OVERSIZED_MAX} words (compilation/livestream) - review/delete"))
        idx[meta["video_id"]] = rec
        n += 1
    if n:
        log(f"flagged {n} oversized (>{OVERSIZED_MAX}w) -> oversized_review.jsonl (SKIPPED)")


# --------------------------------------------------------------------------
#  GPU sampling + server management
# --------------------------------------------------------------------------
def _smi(q):
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return out[0] if out else None
    except Exception:
        return None


class GPUSampler(threading.Thread):
    def __init__(self, interval=2.0):
        super().__init__(daemon=True)
        self.interval, self.samples, self._stop = interval, [], threading.Event()

    def run(self):
        while not self._stop.is_set():
            line = _smi("temperature.gpu,power.draw,memory.used")
            if line:
                try:
                    self.samples.append((time.time(), *[float(x) for x in line.split(",")]))
                except Exception:
                    pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


class Server:
    def __init__(self, lane):
        self.lane = lane
        self.cfg = LANES[lane]
        self.base = f"http://127.0.0.1:{self.cfg['port']}"
        self.proc = None
        self.logf = None
        self.sampler = None

    def start(self, parallel=1):
        cmd = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(self.cfg["port"]),
               "-c", str(self.cfg["ctx"]), "--n-cpu-moe", str(self.cfg["cpu_moe"]),
               "--parallel", str(parallel)] + COMMON_ARGS
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        self.logf = open(OUT_ROOT / f"server_{self.lane}.log", "w", encoding="utf-8", errors="replace")
        self.sampler = GPUSampler()
        self.sampler.start()
        log(f"launch {self.lane} server: ctx={self.cfg['ctx']} cpu_moe={self.cfg['cpu_moe']} "
            f"parallel={parallel} port={self.cfg['port']}")
        self.proc = subprocess.Popen(cmd, stdout=self.logf, stderr=subprocess.STDOUT)
        return self.wait_health(600)

    def wait_health(self, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                log(f"{self.lane} server EXITED early (see server_{self.lane}.log)")
                return False
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=3) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        log(f"{self.lane} server healthy in {time.time() - t0:.0f}s")
                        return True
            except Exception:
                pass
            time.sleep(2)
        log(f"{self.lane} server health TIMEOUT after {timeout}s")
        return False

    def peak(self):
        if not self.sampler or not self.sampler.samples:
            return None, None
        return (max(s[1] for s in self.sampler.samples),
                max(s[3] for s in self.sampler.samples))

    def stop(self):
        if self.proc:
            for fn in (self.proc.terminate, self.proc.kill):
                try:
                    fn()
                    self.proc.wait(timeout=30)
                    break
                except Exception:
                    continue
        if self.sampler:
            self.sampler.stop()
        if self.logf:
            self.logf.close()
        log(f"{self.lane} server stopped")


def chat(base, text, seed, max_tokens):
    body = {"messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": text}],
            "max_tokens": max_tokens, "cache_prompt": False, "stream": False, "seed": seed}
    body.update(SAMPLER)
    body.update(REQ_EXTRA)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/v1/chat/completions", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------
#  Repair one file
# --------------------------------------------------------------------------
def repair_one(base, rel, meta, body, bucket, ctx=16384, halluc=False):
    t0 = time.time()
    chunks = chunk_body(body) if bucket == "huge" else [body]
    outs, sims, gtps, comp_tok = [], [], [], 0
    for ch in chunks:
        resp = chat(base, ch, SEED, out_budget(word_count(ch), ctx))
        msg = resp["choices"][0]["message"]
        content = msg.get("content") or ""
        outs.append(content)
        tim = resp.get("timings", {}) or {}
        usage = resp.get("usage", {}) or {}
        if tim.get("predicted_per_second"):
            gtps.append(tim["predicted_per_second"])
        comp_tok += usage.get("completion_tokens") or 0
        sims.append(norm_sim(ch, content))

    out_text = "\n\n".join(outs)
    lr = len_ratio(body, out_text)
    ns = sum(sims) / len(sims) if sims else 0.0
    flags = []
    if lr < 0.90 or lr > 1.10:
        flags.append(f"len_ratio={lr:.2f}")
    if ns < 0.85:
        flags.append(f"norm_sim={ns:.2f}")

    if halluc and bucket != "huge":
        alt = chat(base, body, ALT_SEED, out_budget(word_count(body), ctx))["choices"][0]["message"].get("content") or ""
        diverge = sorted(set(proper_nouns(out_text)) ^ set(proper_nouns(alt)))
        if diverge:
            flags.append("name_divergence")
            append_review(dict(video_id=meta["video_id"], channel=rel.parts[0],
                               title=meta["title"], divergent=diverge[:40]))

    out_path = OUT_TX / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prov = (f"Title: {meta['title']}\n"
            f"Channel: {rel.parts[0]}\n"
            f"URL: {meta['url']}\n"
            f"CaptionType: auto (repaired)\n"
            f"Repaired: {now_iso()} | model={MODEL_ID} prompt={PROMPT_VER} seed={SEED} "
            f"len_ratio={lr:.3f} norm_sim={ns:.3f} chunks={len(chunks)} "
            f"flags={','.join(flags) or 'none'}\n"
            f"Source: transcripts/{rel.as_posix()}\n\n")
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(prov + out_text, encoding="utf-8")
    os.replace(tmp, out_path)

    rec = dict(video_id=meta["video_id"], channel=rel.parts[0], title=meta["title"],
               source_path="transcripts/" + rel.as_posix(), caption_type="auto",
               word_count=word_count(body), bucket=bucket, status="done",
               output_path="repaired/transcripts/" + rel.as_posix(),
               model=MODEL_ID, prompt_version=PROMPT_VER, seed=SEED,
               len_ratio=round(lr, 3), norm_sim=round(ns, 3), chunks=len(chunks),
               flags=flags, gen_tps=round(sum(gtps) / len(gtps), 1) if gtps else None,
               completion_tokens=comp_tok, wall_s=round(time.time() - t0, 1),
               repaired_at=now_iso(), source_sha256=body_sha(body), error=None)
    append_manifest(rec)
    return rec, flags


def failed_record(rel, meta, body, bucket, err):
    return dict(video_id=meta["video_id"], channel=rel.parts[0], title=meta["title"],
                source_path="transcripts/" + rel.as_posix(), caption_type="auto",
                word_count=word_count(body), bucket=bucket, status="failed",
                output_path=None, model=MODEL_ID, prompt_version=PROMPT_VER, seed=SEED,
                len_ratio=None, norm_sim=None, chunks=0, flags=["error"], gen_tps=None,
                completion_tokens=0, wall_s=0, repaired_at=now_iso(),
                source_sha256=body_sha(body), error=str(err)[:300])


# --------------------------------------------------------------------------
#  Group / lane processing
# --------------------------------------------------------------------------
def process_group(base, group, concurrency, halluc, ctx):
    done = fail = flagged = 0

    def work(item):
        rel, meta, body, bucket = item
        try:
            rec, flags = repair_one(base, rel, meta, body, bucket, ctx, halluc)
            return "done", flags, rel, rec
        except Exception as e:
            frec = failed_record(rel, meta, body, bucket, e)
            append_manifest(frec)
            return "failed", ["error"], rel, frec

    if concurrency <= 1:
        results = (work(it) for it in group)
    else:
        ex = ThreadPoolExecutor(max_workers=concurrency)
        results = ex.map(work, group)

    for status, flags, rel, rec in results:
        done += status == "done"
        fail += status == "failed"
        flagged += bool(flags) and status == "done"
        tag = ("FLAG:" + ",".join(flags)) if (flags and status == "done") else ""
        log(f"  {status:6s} {rec.get('norm_sim')} len={rec.get('len_ratio')} "
            f"{rec.get('gen_tps')}t/s  {rel}  {tag}")
    return done, fail, flagged


def run_lane(lane, pending, args):
    if lane == "small":
        groups = [("short", pending.get("short", []), max(1, args.small_concurrency)),
                  ("medium", pending.get("medium", []), 1)]
        parallel = max(1, args.small_concurrency)
    else:
        groups = [("large", pending.get("large", []), 1),
                  ("huge", pending.get("huge", []), 1)]
        parallel = 1

    total = sum(len(g) for _, g, _ in groups)
    if total == 0:
        log(f"{lane} lane: nothing pending")
        return
    srv = Server(lane)
    if not srv.start(parallel=parallel):
        log(f"{lane} lane: server failed to start - aborting lane")
        srv.stop()
        return
    try:
        d = f = fl = 0
        for gname, group, conc in groups:
            if not group:
                continue
            log(f"{lane}/{gname}: {len(group)} files, concurrency={conc}")
            gd, gf, gfl = process_group(srv.base, group, conc, args.halluc_check, srv.cfg["ctx"])
            d, f, fl = d + gd, f + gf, fl + gfl
        pt, pm = srv.peak()
        log(f"{lane} lane DONE: {d} ok, {f} failed, {fl} flagged | peakT={pt}C peakVRAM={pm}MiB")
    finally:
        srv.stop()


# --------------------------------------------------------------------------
#  Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Batch-repair YouTube auto-captions (two-lane, resumeable).")
    ap.add_argument("--mode", choices=["small", "large", "both", "index"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap files per bucket")
    ap.add_argument("--channel", default=None)
    ap.add_argument("--small-concurrency", type=int, default=1, choices=[1, 2])
    ap.add_argument("--halluc-check", action="store_true")
    ap.add_argument("--reverse", action="store_true", help="largest-first within a group")
    args = ap.parse_args()

    if not args.dry_run and not args.mode:
        ap.error("give --mode {small,large,both,index} (or --dry-run)")

    OUT_TX.mkdir(parents=True, exist_ok=True)
    log("=" * 70)
    log(f"repair_captions start  mode={args.mode} dry_run={args.dry_run} "
        f"small_conc={args.small_concurrency} halluc={args.halluc_check} channel={args.channel}")

    items = list(iter_sources(args.channel))
    autos   = [(p, r, m, b) for (p, r, m, b) in items if m["caption_type"] == "auto"]
    manuals = [(p, r, m, b) for (p, r, m, b) in items if m["caption_type"] == "manual"]
    others  = [(p, r, m, b) for (p, r, m, b) in items if m["caption_type"] not in ("auto", "manual")]
    log(f"enumerated {len(items)} files | {len(autos)} auto | {len(manuals)} manual | {len(others)} other")

    idx = load_manifest()
    pending = {"short": [], "medium": [], "large": [], "huge": []}
    oversized = []
    already = 0
    for p, r, m, b in autos:
        wc = word_count(b)
        if wc > OVERSIZED_MAX:
            oversized.append((r, m, b))
            continue
        if is_done(idx, m, b):
            already += 1
            continue
        bk = bucket_of(wc)
        pending[bk].append((r, m, b, bk))
    for bk in pending:
        pending[bk].sort(key=lambda it: word_count(it[2]), reverse=args.reverse)
        if args.limit:
            pending[bk] = pending[bk][:args.limit]

    # plan
    log("PLAN (pending auto):")
    est_secs = 0.0
    for bk in ("short", "medium", "large", "huge"):
        g = pending[bk]
        if not g:
            continue
        ws = [word_count(it[2]) for it in g]
        est_secs += sum(w * 1.3 for w in ws) / LANES[LANE_OF[bk]]["tps"]
        log(f"  {bk:6s} -> {LANE_OF[bk]:5s} lane: {len(g):4d} files  "
            f"words avg={sum(ws) // len(ws)} max={max(ws)}")
    n_manual_new = sum(1 for _, _, m, _ in manuals
                       if idx.get(m["video_id"], {}).get("status") != "skipped_manual")
    n_over_new = sum(1 for _, m, _ in oversized
                     if idx.get(m["video_id"], {}).get("status") != "skipped_oversized")
    log(f"  oversized (>{OVERSIZED_MAX}w -> SKIP+review): {len(oversized)} ({n_over_new} new)")
    log(f"  already done: {already} | manual to index: {n_manual_new} | "
        f"rough compute est: {est_secs / 3600:.1f}h (excl. prompt prefill + model load)")

    if args.dry_run:
        log("dry-run: no server, no writes. done.")
        return

    index_manual(manuals, idx)
    index_oversized(oversized, idx)
    if others:
        log(f"note: {len(others)} files lacked an auto/manual flag (unexpected outside Blueprint) - skipped")

    if args.mode == "index":
        log("index mode: manual map written; auto entries land as they complete. done.")
        return

    lanes = {"small": ["small"], "large": ["large"], "both": ["small", "large"]}[args.mode]
    for lane in lanes:
        run_lane(lane, pending, args)
    log("all requested lanes complete.")


if __name__ == "__main__":
    main()
