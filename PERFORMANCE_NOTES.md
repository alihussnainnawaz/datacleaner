# Performance Overhaul — What Changed and Why

## Update 2: real-time progress API, and the actual cause of "extra 3 minutes"

### The fake ticker is gone — replaced with a real polling API

Previously, the pipeline popup's live view (while a run is in flight) was
driven entirely by a client-side `setInterval` that faked progress based on
step "weights" — none of the numbers shown *during* a run were real; only
the numbers shown *after* the response came back were measured. That's now
replaced end-to-end:

- **`progress_store.py`** (new): a thread-safe, in-memory store of real step
  boundaries for a running pipeline call. The cleaning engine and validation
  engine report genuine `time.perf_counter()`-measured start/end events into
  it as they execute — from the worker thread actually doing the work.
- **`GET /api/clean/progress/{file_id}`** (new): the frontend polls this
  every 350ms while a run is in flight. Every field in the response is a
  real measurement: which step is executing right now, how long it's been
  running so far, and the real elapsed seconds for every step that's
  already finished.
- **Frontend**: `_tickProgress()` (the fake weight-based animation) is
  deleted. `_pollProgress()` replaces it — it reads the live snapshot from
  the backend every poll and updates each row directly from real numbers.
  Nothing is estimated or animated.
- Verified end-to-end with a concurrent test (pipeline running in one
  thread, `/progress` polled from another): the live "current step" and its
  live elapsed time exactly track what the backend is actually doing, and
  the final `done` list matches the post-hoc `step_timings` in the pipeline
  response exactly.
- Also added a real, separate timer for "Cleaning special chars" — it used
  to be silently bundled inside the geo/category steps with no timer of its
  own, meaning its row could never show a real number even after the first
  round of instrumentation. It now has one.
- One honest limitation: `clean_dataframe_banks` / `clean_dataframe_certificates`
  don't have the same per-sub-step instrumentation as the beneficiary
  pipeline (`clean_dataframe_fast`) — they report one coarse "cleaning"
  start/end pair rather than a live update per column-level step. Real
  number, just coarser granularity for those two data types.

### The real cause of the "extra 3 minutes": a bug I introduced, not disk storage

Investigating "I don't want Validating File to run every time, and I don't
want the upload stored" turned up two concrete, fixable bugs — and neither
one is actually "storing the file on disk":

1. **Every successful pipeline run deleted the uploaded file from disk**
   (`delete_file(file_id)` at the end of `_run_clean` in `routes_clean.py`).
   That meant a *second* "Run Pipeline" click on the same upload — e.g.
   after tweaking a filter or column rule — returned a flat 404 ("No file
   found for file_id"), forcing a full re-upload (network transfer +
   re-parse) just to try again. Verified directly: two pipeline calls in a
   row on the same `file_id`, second one 404'd before the fix, succeeds
   after it. **This is almost certainly the actual "extra 3 minutes"** if
   you were iterating on rules and re-running.
2. **A regression from the earlier memory-optimisation pass**: after
   cleaning, the code evicted the parsed-file cache (`_DF_CACHE`) to save
   memory during that one request. Combined with bug #1 not existing yet at
   the time, this seemed harmless — but it meant that even if the file
   *hadn't* been deleted, every subsequent run would still fully re-parse
   the file from scratch instead of hitting cache.

Both are fixed: the raw upload is no longer auto-deleted after a run (it's
still deletable on request via `DELETE /api/upload/{file_id}`), and the
parse cache is no longer evicted, so it stays warm across runs. Verified
directly: uploading once and running the pipeline three times in a row now
shows a real, measured **0.0s** file-load stage on every run after the
first (the upload endpoint already parses+caches the file immediately to
report row/column counts back to you, so even the *first* pipeline run
usually sees a warm cache).

**On "no need to store the upload data in the folder":** the disk write
itself was not the source of the delay — it's a single one-time write per
upload (a few seconds even for a large file), and reading from a warm
in-memory cache costs nothing regardless of whether the source bytes also
happen to be sitting on disk. Removing disk storage entirely would be a
larger architectural change (the regex/mapping-rule tools rewrite the raw
file on disk in place, and keeping it means a project can be resumed after
a server restart) with real tradeoffs, and — now that the actual bug is
fixed — it isn't what was costing the time. Happy to go further and move to
a pure in-memory upload (no disk write at all) if it's still wanted, but
wanted to flag the tradeoff plainly rather than rip out durable storage
without discussing it first. The parse-cache is bounded (max 8 most-recently-used
files) so it can't grow unbounded across a long-running server session
either way.

---

## Update (post-review): three more "runs without asking" issues fixed

A follow-up review of the refactored code turned up three more places where
behavior ran without being explicitly configured on the frontend:

1. **Dead "special" toggle, now wired up.** The frontend's global-rules
   "special" checkbox (special-character stripping) was never read by the
   backend at all — only "trim" and "null" were. Toggling it had zero effect;
   special-char stripping ran unconditionally as a fixed part of the
   geo/string-category cleaning steps. Fixed: `global_rules.get("special",
   True)` is now threaded through `_step_geo` / `_step_string_category` in
   all three pipelines (beneficiary, banks, certificates) via a `run_special`
   parameter, so unchecking it actually skips that sub-step.

2. **Two unconditional structural checks removed.** `_step_duplicate_rows`
   (flags fully-duplicate rows) and `_step_type_mismatch` (beneficiary only —
   heuristically flags cells that look like the wrong data type) used to run
   on every pipeline call with no frontend control and no way to disable
   them. They only ever wrote "needs manual review" flags (never mutated
   cell values), but they were never something the user asked for. Both
   calls have been removed from all pipelines; the function definitions are
   left in the file, unused, in case they're wanted as an explicit opt-in
   filter later.

3. **"Auto Clean" button and endpoint removed entirely.** This was a second,
   separate code path (`/api/clean/tools/auto-clean`, a "Auto Clean" button
   in the Clean Tools tab) that called the cleaning engine with **no
   `enabled_rules` argument at all** — bypassing Column Rules gating
   completely and re-applying the old always-on legacy behaviour (CNIC
   formatting, gender normalisation, hardcoded bank-name fuzzy-matching,
   hardcoded geo canonical-list matching, casing, date auto-detection) to
   every column that matched the static schema, regardless of what was
   configured in Column Rule Preview. It was also already silently broken
   post-refactor (it read the old per-row dict result shape, which no longer
   exists). Removed: the backend route, the frontend button, the
   `runAutoCleanOnly()` / `_renderAutoBanner()` JS functions, and the
   associated dead CSS.

With these three fixes, the only things that run without an explicit
per-column Column Rule are the two global toggles (**trim**, **null
standardisation**) — both visible checkboxes in the UI, both defaulting to
on but fully user-controllable — and now **special** joins them as a real,
working toggle. Everything else (CNIC formatting, casing, bank/geo
fuzzy-matching, date standardisation, gender/bool normalisation, etc.) only
runs on a column if the user explicitly assigned that rule to it in Column
Rule Preview. The R01–R12 predefined auto-validation rules remain off by
default and are never triggered by the frontend (this was already fixed in
the original performance pass — see below).

---

Target: clean + validate ~700k rows with 16 cleaning + 16 validation filters in
seconds-to-tens-of-seconds instead of 45 minutes, with byte-identical outputs,
real (not fabricated) per-step timings in the pipeline popup, and a readable
"Validation Status" column in the cleaned file.

All changes are output-equivalent: verified by automated comparison against the
untouched legacy engine (cleaned values, filter flag counts and row lists,
report file contents including original/cleaned/review JSON and uuid keys,
per-row PASS/FAIL — zero mismatches at test scale).

## The four root causes of the 45-minute runs

1. **Per-cell Python string ops.** The engine forced pandas' "python" string
   storage, so every `.str.strip/replace/title/...` ran a Python call per cell:
   ~23M cells x dozens of passes. Fixed by switching to pyarrow-backed strings
   (C++ kernels), replacing the one RE2-incompatible backreference regex with an
   exact isin() set, and passing pattern strings instead of compiled `re.Pattern`
   objects (compiled patterns silently force the slow Python fallback).

2. **Per-row bookkeeping at O(rows), not O(changes).** The cleaning result
   materialised a dict per row (690k dicts) and validation built one entry dict
   per filter PER ROW (16 x 690k = 11M dicts), then JSON-dumped a ~2KB status
   blob for EVERY row (~1.4GB of strings — this alone explains swapping/45-min
   runs on RAM-tight machines). Replaced with:
   - `_CleanLog`: columnar change records (col, int64 idx array, factorised
     values) — O(1) per step, memory O(changed cells) with values deduplicated.
   - Failure-sparse validation status: masks stay vectorised; per-row JSON is
     composed from prebuilt per-filter string fragments ONLY for failing rows;
     all-pass rows store the literal "PASS".
   - `write_outputs` builds the report from the columnar logs (JSON only for
     touched rows); `_summarise` computes counts from numpy arrays.

3. **Redundant full-dataset copies.** Removed: the second deep copy in each
   pipeline (input frame is read-only and serves as `original`), the deep copy
   in `run_validation` (only adds a column), the copy in the output writer
   (columns are replaced, never mutated). Touched-cell originals are captured
   eagerly (factorised) so the upload cache is evicted right after cleaning.
   The coordinate raster is downcast to int16 (~765MB → ~380MB). Net peak
   memory is roughly halved vs. legacy.

4. **Slow per-value work that repeats.** Unique-value memoisation everywhere it
   is exactly equivalent: date parsing (`format="mixed"` infers per element, so
   parsing distinct values once is identical), trim/special-chars/casing via an
   arrow-native dictionary_encode → transform-dictionary → take pipeline, and
   the raster gap-search deduplicated by grid cell with a process-wide cache.
   `_norm`/`_parse_dates` in the validation engine are arrow-backed/memoised.

## Honest timings (in the popup and the API)

- Backend now measures real wall-clock per stage (`clean`, `validate`, `write`,
  `total`), per cleaning step (keyed to the popup catalog), and per validation
  filter, returned as `step_timings` in the pipeline response.
- The frontend popup displays those measured numbers; steps the backend didn't
  measure individually show "—". The old proportional weight split remains only
  as a fallback for responses without timing data.

## Output contract

- The cleaned file now always contains a human-readable **"Validation Status"**
  column: `PASS` or `FAIL: <failed filter labels joined by " | ">`, composed in
  `run_validation` where the labels are already known. The internal
  `__validation_status__` JSON column remains for the in-app report page
  (`strip_validation_status_column.py` can remove it for delivery if desired).
- The report and duplicates files are still produced for the in-app report UI,
  but the cleaned file is self-contained — the second download is no longer
  needed to see validation results.
- `flagged_rows` in the summary is capped at 100k rows per filter (counts stay
  exact) so a degenerate filter cannot bloat the response/sidecar.

## Other

- xlsx writers switched to xlsxwriter `constant_memory` (~1.5x faster than
  openpyxl write_only, flat memory), with openpyxl fallback.
- `pyarrow` and `xlsxwriter` added to requirements.
- Predefined-rules path, banks and certificates pipelines share all of the
  above (single fast result contract with automatic legacy fallback in
  `_summarise` / `write_outputs`).

## Measured (single-core 2.8GHz sandbox, 4GB RAM, worst-case synthetic data
   with ~30% dirty cells — real data is cleaner and machines faster)

| Stage                          | Legacy (extrapolated) | Now (measured) |
|--------------------------------|----------------------:|---------------:|
| Cleaning, 690k x 34 cols       | ~180s+                | ~36s           |
| Validation, 16 filters, 690k   | ~180s+                | ~9.5s          |
| Status column memory           | ~1.4GB                | O(failures)    |
| Report build                   | O(rows) dicts + json  | O(touched)     |

On a typical multi-core desktop these numbers shrink further (2-3x from clock
and cache alone). If runs are still slow on the target machine, check RAM
headroom first — swapping, not CPU, is what turns minutes into three quarters
of an hour.
