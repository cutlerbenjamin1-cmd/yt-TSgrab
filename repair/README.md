# Caption repair

A batch harness that takes the scraper's **auto-generated** transcripts and runs
each one through a local LLM to restore punctuation, capitalization, sentence and
paragraph structure, and to fix the word errors ASR leaves behind - without
summarizing, paraphrasing, or inventing anything. Manual captions are already
clean, so they're skipped (indexed as passthrough) and never touched.

It reads `transcripts/<Channel>/*.txt` from the repo root, writes repaired copies
to `repaired/transcripts/**`, and keeps a resumeable JSONL manifest. It never
writes to the source tree, so it's safe to run alongside a live scrape.

The model and settings here weren't guessed - they're the output of a
model bake-off and a prompt A/B, both summarized below.

---

## Why

Auto-captions come off YouTube as a wall of lowercase, unpunctuated text with
run-together and mis-heard words (`h 100red` for "hundred", `aasbo` for
"Arecibo"). Manual captions are clean. Repairing only the auto ones makes the
whole corpus read consistently, which is the point if you're building a
searchable/readable archive.

The job is deliberately narrow: **copy-edit, don't rewrite.** The output must
be the same words the speaker said, just legible. That constraint drives every
choice below - the model pick, the sampler, the prompt, and the faithfulness
checks.

---

## The pick: Qwen3.6-35B-A3B-MTP (UD-Q4_K_M)

A Mixture-of-Experts model: 35B total, ~3B active per token. It fits a single
24 GB GPU and, with MTP speculative decoding, runs this task at **155-166
gen tok/s** at 61-65 °C on an RTX 3090. It recovers famous mangled names
(`aasbo` → Arecibo, `Carl San` → Sagan) and stays faithful to the source.

Lighter alternative: **Qwen3-4B-Instruct-2507** (Q8) - 117 tok/s, 60 °C, still
recovers famous names. Best small footprint.

Most conservative: **gpt-oss-20b** - fast, cool, faithful, but it leaves mangled
names *untouched* (it won't guess). That caution is a feature on corpora full of
obscure names. See the benchmark for the full picture.

---

## How the harness works

1. **Enumerate** `transcripts/**/*.txt`, parse the 7-line header, keep only
   `CaptionType: auto`. Manual files are recorded as passthrough; files over
   80,000 words (12-hour compilations / livestream re-uploads) are skipped to a
   review list.
2. **One server, kept warm** across every file (never reloaded per file).
3. **One-shot per file** - no context carried between files. System prompt =
   `prompt.txt`; user message = the raw caption body with its header stripped.
4. **Two lanes**, because only one ~22.7 GB model fits VRAM at a time:
   - **small** - ctx 16384, `--n-cpu-moe 2`, port 8181 → files ≤ 6000 words.
   - **large** - ctx 32768, `--n-cpu-moe 4`, port 8182 → 6000-11000 words
     one-shot; longer files are **chunked** on line boundaries (~6000 words each)
     and stitched back together.
5. **Faithfulness guardrails** on every output: length ratio and a
   punctuation-insensitive similarity; anything anomalous is flagged.
6. **Resumeable manifest** (`repaired/manifest.jsonl`), keyed on `video_id`,
   last-record-wins. A finished file is skipped on re-run unless its source text
   changed (tracked by SHA-256). This manifest is also the artifact a later step
   uses to merge repaired-auto with the gold manual captions.

Every repaired file gets a small provenance header (model, prompt version, seed,
`len_ratio`, `norm_sim`, flags) so you can always see how it was produced.

---

## Benchmark: how the model was chosen

**Setup.** RTX 3090 (24 GB), `llama-server` build b9627, GGUF weights (Q8 for
small models, Q4_K_M for large). 11 models in round 1, expanded to 17+
model/configs in a MoE-focused round 2. Each model ran the same two transcripts,
one-shot, at a task-faithful sampler (see below).

**Test material.**
- A **short, hard** clip (Be Smart, *"50 years ago we phoned E.T."*) that hinges
  on recovering mangled famous names - `aasbo` → **Arecibo**, `Carl San` →
  **Sagan** - from context.
- A **long** transcript (Be Smart, *Golden Ratio*) for throughput and to check
  the model doesn't summarize or pad over length.

**What was measured.**
- **Throughput** - generation tokens/sec on the long transcript.
- **Heat** - peak GPU temperature (this runs for hours; thermals matter).
- **Name recovery** - did it fix the two mangled famous names, or leave/mangle them?
- **Faithfulness** - `len_ratio` (output words ÷ input words; ~1.0 = no
  summarizing/padding) and a punctuation-insensitive word-sequence similarity.
- **Quality** - read by eye. There is **no gold reference**, so quality is a
  judgment call, not a score. This is the main methodological caveat: the
  numbers are exact, the "good/bad" is human.

### Results

Long-transcript throughput, peak temperature, and famous-name recovery:

| Model | Type | gen tok/s | peak °C | aasbo→Arecibo | Carl San→Sagan |
|-------|------|----------:|--------:|:---:|:---:|
| Qwen3-0.6B | dense | 310 | 60 | ✗ | ✗ |
| Qwen3-1.7B | dense | 218 | 63 | ✗ | ✓ |
| Qwen2.5-3B | dense | 169 | 67 | ✓ | ✓ |
| Llama-3.2-3B | dense | 134 | 61 | ✗ | ✓ |
| Qwen3-4B-Instruct-2507 | dense | 117 | 60 | ✓ | ✓ |
| Qwen3-8B | dense | 82 | 74 | ✗ | ✓ |
| gpt-oss-20b | MoE | 104 | 63 | ✗ | ✗ |
| Ministral-14B | dense | 52 | 67 | ✓ | ✓ |
| Qwen3.6-27B | dense | 42 | 77 | ✓ | ✓ |
| Qwen3-30B-A3B-Instruct-2507 | MoE | 164 | 66 | ✓ | ✓ |
| Qwen3.5-35B-A3B | MoE | 135 | 70 | ✓ | ✓ |
| **Qwen3.6-35B-A3B-MTP** *(pick)* | MoE | **155-166** | **61-65** | ✓ | ✓ |

*(Qwen3.6-35B-A3B-MTP shown at its production config: ctx 16384, `--n-cpu-moe 2`,
MTP on. Full-GPU at ctx 10240 it hits 188 tok/s.)*

### Key findings

1. **Fewer *active* params win this task.** The MoE models (30B-A3B, 35B-A3B,
   both ~3B active) beat the dense 27B on speed *and* heat while matching its
   quality. Copy-editing doesn't need a big dense forward pass.
2. **MTP is nearly free here.** Caption repair is an almost perfectly
   predictable workload for speculative decoding - draft acceptance measured at
   **~99%** - so the multi-token-prediction head gives a real speedup (≈1.3× on
   this MoE, ≈1.8× on a dense model) at a lossless output. Turn it on.
3. **gpt-oss-20b is faithful but *conservative*.** It's fast and cool and never
   hallucinates, but it also refused to touch `aasbo`/`Carl San` that even a
   1.7B model half-fixed. That makes it the safe choice for **obscure-name**
   corpora and the wrong choice when you want names recovered.
4. **"Old/tiny is fine" holds - up to a point.** A 2024 Qwen2.5-3B recovered
   both names that gpt-oss-20b missed. Reliable famous-name recovery kicks in
   around the **4B** mark (Qwen3-4B-2507 is the efficiency winner: coolest, both
   names, tiny footprint).
5. **All models stayed faithful on length** (`len_ratio` 0.97-1.00) - none
   summarized the long transcript. Llama-3.2-3B was the exception on *structure*:
   it over-restructures and reorders sentences.

---

## The prompt

Four system-prompt variants were A/B'd across three high-signal transcripts.
The winner (`prompt.txt`, "v1 conservative") adds an explicit proper-noun clause:
fix ordinary mis-heard common words normally, but only correct a proper noun when
the ASR spelling is a **confident** phonetic match to a specific name that fits
the context - otherwise keep the ASR text verbatim.

Effect: every famous-name recovery and the faithfulness numbers were preserved,
but **confident-wrong real-name substitutions turned into safe phonetic keeps**.
A prompt that asked the model to *flag* its own uncertain names failed - the
model only brackets what it already feels unsure about, and the bad guesses are
exactly the ones it feels sure about. Miscalibration can't be prompted away.

---

## Known failure mode (read this before trusting it)

The model **nails famous names** (Jan Palach, Brezhnev, Solzhenitsyn,
Milankovitch, Diego Garcia, Croatoan, Virginia Dare) and **guesses wrong on
obscure/mangled ones**, roughly 1-2 per transcript. The worst observed case:
a mangled "...Mediterranean at [jalar]..." - correct answer **Gibraltar** - came
out as **"Jalalabad"** (a real, confident, geographically impossible
substitution).

These wrong guesses are **low-confidence and sampler-dependent**: correct
famous-name recoveries are stable across seeds, but the bad guesses *change*
when you change the seed (`Jalalabad` → `Jajce`). That gives a cheap detector,
built into the harness as `--halluc-check`: re-run at a second seed and flag any
proper noun that diverges for human review.

No prompt makes the model *know* a name it can't read from mangled audio - the
only real fix for those is re-transcribing from the **audio** with something like
Whisper large-v3, which this harness does not do. Practical guidance:

- **Famous-topic explainer channels** → production-grade as-is.
- **Niche / obscure-name content** → run `--halluc-check`, spot-check the flags,
  or use the conservative gpt-oss-20b instead.

Feeding a **manual** (already clean) transcript through it is safe - it makes no
things up, applies only light restyling, and even catches residual typos - but
it's not free (~10-15 s each), which is why manual files are skipped by default.

---

## Requirements

- A GPU that fits your chosen model. Reference: single **RTX 3090 (24 GB)**.
- An OpenAI-compatible `llama-server` (`/v1/chat/completions`). For the reference
  model you need a **llama.cpp build with MTP** (`--spec-type draft-mtp`); drop
  those two flags in `COMMON_ARGS` and any server works.
- The model GGUF. Reference: **Qwen3.6-35B-A3B-MTP UD-Q4_K_M** (~22.7 GB).
  Lighter: **Qwen3-4B-Instruct-2507** Q8.
- **Python 3.9+**, standard library only - no dependencies beyond the scraper's.

---

## Running it

1. Open `repair_captions.py` and set the two paths under
   `# --- EDIT THESE ---`: `SERVER_BIN` (your `llama-server.exe`) and `MODEL`
   (your GGUF).
2. **Dry run first** - no GPU, no writes. It prints the plan (files per bucket,
   rough compute estimate) and how many are already done:
   ```
   python repair_captions.py --dry-run
   ```
3. Full run - small lane then large lane:
   ```
   python repair_captions.py --mode both      # or: repair.bat
   ```

| Flag | Effect |
|------|--------|
| `--mode {small,large,both,index}` | which lane(s) to run; `index` writes the manual/oversized map with no GPU |
| `--dry-run` | plan + already-done count, no GPU, no writes |
| `--limit N` | cap files per bucket (smoke tests) |
| `--channel NAME` | restrict to one channel folder |
| `--small-concurrency {1,2}` | fire 1 (default) or 2 short jobs at once |
| `--halluc-check` | re-run each file at a 2nd seed, flag seed-divergent proper nouns |
| `--reverse` | process largest files first within a group |

A **serial** small lane is intentionally the default: MTP speculative decoding
already saturates the GPU, so running two files at once was *slower* in testing
(132 vs 166 tok/s aggregate), not faster.

**Long runs** should be launched detached so they survive the shell:

```
powershell -NoProfile -Command "Start-Process python -ArgumentList 'repair_captions.py --mode both' -WindowStyle Hidden -WorkingDirectory 'PATH\TO\repair'"
```

The harness self-logs to `repaired/repair.log`; poll that and `manifest.jsonl`
for progress. It's stoppable and resumeable at any time - a re-run skips
everything already done.

### Reference server command

What the harness launches for the small lane (the large lane is `-c 32768
--n-cpu-moe 4`):

```
llama-server -m <MODEL.gguf> -ngl 99 -c 16384 --jinja -fa on \
  --spec-type draft-mtp --spec-draft-n-max 2 --n-cpu-moe 2 \
  --cache-type-k q8_0 --cache-type-v q8_0 --host 127.0.0.1 --port 8181
```

Sampler (per request): `temperature 0.2, top_p 0.8, top_k 20, min_p 0.0`,
`seed 42`, thinking disabled. Low temperature and no repetition penalty on
purpose - a transcript legitimately repeats words, and higher temperature hurts
verbatim faithfulness.

---

## Output

```
repaired/
├── transcripts/<Channel>/<Title>.txt   repaired text + provenance header
├── manifest.jsonl                       one record per video (incl. manual) - resume + merge artifact
├── review_flags.jsonl                   files flagged by faithfulness / --halluc-check
├── oversized_review.jsonl               files skipped for being > 80k words
├── repair.log                           run log
└── server_{small,large}.log             llama-server output per lane
```

Each `manifest.jsonl` record carries: `video_id, channel, title, source_path,
caption_type, word_count, bucket, status, output_path, model, prompt_version,
seed, len_ratio, norm_sim, chunks, flags[], gen_tps, completion_tokens, wall_s,
repaired_at, source_sha256, error`.

**Merge (later).** Because manual files are indexed too, producing a single clean
corpus is one pass over the manifest, per `video_id`:

- `caption_type == manual` → use `source_path` (already gold)
- `caption_type == auto` and `status == done` → use `output_path` (repaired)
- `status == skipped_oversized` → review

All of `repaired/` is runtime output and is git-ignored.

---

## Tuning knobs

All at the top of `repair_captions.py`:

- **`--n-cpu-moe`** (per lane, in `LANES`) - the VRAM lever. `0` = full-GPU
  (fastest, but needs a smaller ctx to fit); higher offloads more experts to CPU
  RAM to free VRAM for KV cache. `2`/`4` are the tuned values for the 3090.
- **Q8 KV cache** (`--cache-type-k/v q8_0`) - cheap here; this model's hybrid
  attention keeps the KV footprint small, so the *model file* is the VRAM hog,
  not the cache.
- **Bucket thresholds / chunk size** - `SHORT_MAX`, `SMALL_MAX`, `HUGE_MIN`,
  `OVERSIZED_MAX`, `CHUNK_WORDS`.
- **Sampler / seed / prompt version** - `SAMPLER`, `SEED`, `PROMPT_VER`.

To run a **different model** (e.g. the lighter Qwen3-4B or the conservative
gpt-oss-20b), point `MODEL` at its GGUF, update `MODEL_ID`, and - if it has no
MTP head - remove `--spec-type draft-mtp --spec-draft-n-max 2` from
`COMMON_ARGS`.
