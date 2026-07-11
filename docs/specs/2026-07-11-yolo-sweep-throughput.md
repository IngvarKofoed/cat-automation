# Speed up the offline analysis sweep (de-starve the GPU)

The offline YOLO oracle sweep (`compute/analysis/runner.py`) runs at ~4.5 fps
(~8 h for 135 k frames) on the real compute box (RTX 5060 Ti, CUDA) with the GPU
only ~35 % utilized. It is **starved**, not compute-bound: the loop is fully
serial — per-frame disk read → `cv2.imdecode` → `model.predict(batch=1, FP32)` →
per-frame `commit()` on a rollback-journal / `synchronous=FULL` connection — so
the GPU idles during every read, decode, and commit. This spec adds a **batched,
prefetched** execution path for *stateless* oracles (only YOLO today) plus cheaper
store writes, to raise utilization and throughput ~2–4×. The windowed path
(BSUV/MOG2) is untouched. Verdicts are **preserved by construction and confirmed
by a one-time batched-vs-serial validation** (see *Verdict preservation &
validation*); the only lever that can meaningfully move them — opt-in FP16 — is off
by default.

## Key decisions

- **`analyze_batch()` is an optional method on the `Analyzer` contract, not a base
  class** (extends). `compute/analysis/base.py`'s `Analyzer` is a structural
  `Protocol`, so it can't hand backends a default body. Instead the runner uses a
  small helper that calls `analyzer.analyze_batch(images)` when present and
  otherwise loops `analyze()` per image. Only `YoloAnalyzer` overrides it; every
  existing backend keeps working with no edit.
- **The batched path is stateless-only; windowed stays exactly as-is** (reuses the
  `windowed` split). The runner branches on `analyzer.windowed` as it does today.
  `windowed=True` (BSUV/MOG2) keeps the strict-time-order per-frame loop — batching
  or reordering would corrupt its rolling background. Only `windowed=False` gets
  prefetch + batching.
- **One prefetch producer thread decodes ahead of the GPU** (new). A daemon thread
  pulls `(frame_id, abs_path)` from the existing `iter_unanalyzed` iterator, reads
  + decodes each frame, and puts `(frame_id, image)` onto a **bounded** queue; the
  main thread pulls batches and runs inference. Decode (which releases the GIL in
  OpenCV) thus overlaps GPU inference. One producer is enough to stay ahead of the
  GPU; the queue bound caps memory.
- **Batched verdict commits + WAL** (extends the store). New
  `Store.write_analysis_batch(rows)` does one `executemany` of the same
  `INSERT OR REPLACE … WHERE EXISTS` guard under one lock hold + one `commit()`.
  The connection opens in `journal_mode=WAL`, `synchronous=NORMAL`, making each
  commit cheap (fsync deferred to checkpoint). This is a **store-wide** durability
  change — the always-on collector commits through the same connection.
- **FP16 is opt-in and cuda-gated** (new). `CAT_YOLO_HALF` (default **off**) passes
  `half=True` to `predict()` only when the device is `cuda`. It is the lever most
  likely to move a verdict near the `conf=0.15` floor, so it stays off by default;
  on MPS/CPU it is ignored. (Batching can *also* nudge verdicts marginally — see
  *Verdict preservation & validation* — which is why the guarantee is validated,
  not asserted.)
- **Per-frame log-and-skip is preserved, one layer deeper** (reuses the invariant).
  A decode failure is *detected* in the producer but *counted and logged* by the
  main thread (via a skip-marker on the queue), so the throttled cadence and skip
  total stay single-owner. A *batch* inference error falls back to per-image
  inference within that batch, so one bad frame can never drop a whole batch of good
  verdicts nor abort the sweep.

## Goals

- Raise GPU utilization well above 35 % and cut sweep wall-clock ~2–4× on the CUDA
  box, for `windowed=False` oracles (YOLO).
- **Verdict-preserving by default, empirically validated.** Prefetch and batched
  commits are bit-identical; batching is verdict-preserving in principle but not
  provably bit-identical (cuDNN/letterbox — see below), so it is confirmed by a
  one-time batched-vs-serial diff. Only the opt-in FP16 flag is expected to move
  any verdict, and it is off by default.
- Preserve every existing invariant: the windowed-vs-stateless split,
  `iter_unanalyzed` resumability, idempotent verdict writes, per-frame
  log-and-skip, and the eviction-race frame-existence guard.

## Non-goals

- Speeding up the **windowed** path (BSUV/MOG2). Its sequential contract makes
  batching unsafe; out of scope.
- TensorRT / ONNX export, model-size or `imgsz` changes, or any recall/precision
  tuning — those trade accuracy and are separate levers (the env vars already
  exist).
- Multi-GPU, multi-process, or distributed sweeps.
- Changing the client-side ETA/progress logic (changelog 39) — it keys on
  `manager.record()`, which still fires per verdict.

## Design

### The `Analyzer` contract (`compute/analysis/base.py`)

Document an **optional** `analyze_batch(images: list[np.ndarray]) -> list[AnalysisResult]`
on the `Analyzer` Protocol: verdicts order-aligned to `images`, semantically
identical to calling `analyze()` on each. The Protocol only *documents* it; the
runner never assumes it exists. A module helper in `runner.py`:

```python
def _batch_analyze(analyzer, images):
    fn = getattr(analyzer, "analyze_batch", None)
    return fn(images) if fn is not None else [analyzer.analyze(im) for im in images]
```

is the "default" — so a future stateless backend without `analyze_batch` still
runs correctly (just unbatched).

### `YoloAnalyzer` (`compute/analysis/yolo.py`)

- Factor the model call into one `_predict(images: list)` wrapper (fixed
  `imgsz`/`conf`/`classes=[15]`/`device`/`half=self._half`/`verbose=False`) and the
  box-extraction tail into `_result_from(result)`. `analyze_batch(images)` =
  `[_result_from(r) for r in self._predict(images)]` — ultralytics returns one
  `Results` per input, in order. `analyze(image)` = `analyze_batch([image])[0]`.
  Both go through the *same* `_predict`, so the `_batch_analyze` default fallback and
  the per-image retry of a failed batch run under the **same `half` regime** — an
  OOM'd FP16 batch never silently retries in FP32 (the regime-mixing hazard below).
- Read `CAT_YOLO_HALF` in `__init__` (same precedence as the other env vars),
  store `self._half`. In `prepare()`, if `self._half` and device ≠ `cuda`, disable
  it and log once (FP16 gives nothing on CPU/MPS). Add `"half": self._half` to
  `detail` for provenance.
- **Toggling `half` mid-store blends regimes.** FP16 and FP32 verdicts both land in
  the same `(frame_id, 'yolo')` rows, distinguishable only by per-row
  `detail.half`; scorecard/disagreement queries key on `analyzer` alone and would
  silently mix two model regimes. Guidance: treat a `half` change like a model
  change — do a full `reanalyze` of the affected window so a scored window is
  single-regime.
- New env `CAT_YOLO_BATCH` (default **8**), read in `__init__` and exposed as
  `self.batch_size` — the attribute the runner reads (`getattr(analyzer,
  "batch_size", 1)`) to size batches and the prefetch queue. 8 is a safe fit for
  `yolo11x` @ imgsz 1280 FP32 on 16 GB VRAM — conservative to avoid CUDA OOM out of
  the box, tunable up (esp. with FP16, which halves activation memory).

### The runner (`compute/analysis/runner.py`)

`run_analysis` keeps its scope resolution, `prepare`, `reanalyze` clear, and
iterator/`total` selection unchanged. Only the **execution** of the stateless
branch changes:

- **Windowed** (`analyzer.windowed`): unchanged serial per-frame loop.
- **Stateless**: start one daemon **producer** thread over `iter_unanalyzed(...)`
  that read+decodes each frame and puts `(frame_id, image)` on a bounded
  `queue.Queue(maxsize = 2 × batch_size)`. The main thread drains the queue into
  batches of `batch_size` and, per batch: `results = _batch_analyze(analyzer,
  images)`, then `store.write_analysis_batch([...])` and `manager.record(v)` per
  verdict. **`batch_size` is an attribute the analyzer exposes** (`YoloAnalyzer`
  sets it from `CAT_YOLO_BATCH`); the runner reads `getattr(analyzer, "batch_size",
  1)`, so a future stateless analyzer without it simply runs unbatched, and the
  queue bound is always well-defined.

- **Producer lifecycle — no error path may wedge the job.** The current serial loop
  lets a fatal error (`iter_unanalyzed` raising — `sqlite3`, a keyset bug) propagate
  to `_run`, which records it in `status().error`. Moving iteration onto a thread
  must preserve that or a blocked `queue.get()`/`queue.put()` hangs forever,
  `running` stays `True`, and the whole one-at-a-time job queue wedges. Three exit
  paths, all handled — the key is a **per-job internal `abort` `threading.Event`**
  (distinct from the user-facing `stop_event`) that couples the two threads:
  - **Producer-fatal** (iterator/read raises): the producer's `try/except/finally`
    captures the exception into a shared slot and its `finally` **always** enqueues
    the sentinel, so the main loop's `get()` never blocks; after draining to the
    sentinel the main loop re-raises the captured error, surfacing through `_run`
    into `status().error` as today.
  - **Consumer-fatal** (main loop raises — e.g. `write_analysis_batch` sqlite I/O):
    `run_analysis`'s own `finally` **sets `abort` and drains the queue before
    `join()`**. The producer's `put()` uses a timeout and checks `abort` each turn,
    so a producer parked on a full queue unblocks instead of hanging the join.
    Without this the consumer-error path deadlocks — the failure this section
    exists to prevent.
  - **Cancel** (`manager.stop_event`): checked by the main loop between batches and
    by the producer's put loop; on cancel the main loop drains/discards the queue
    and joins. (Cancel and consumer-fatal share the same drain-then-join teardown.)
  - The main thread **`join()`s the producer before `run_analysis` returns** on
    every path, so no orphan producer survives to run alongside the next job. Cancel
    latency is ~1 batch (< ~1 s) — acceptable.

- **Skip accounting stays single-threaded.** A decode failure in the producer is
  enqueued as a skip-marker `(frame_id, None, exc_text)` carrying the formatted
  exception; the **main thread** owns the one `errors` counter, the throttled
  `_LOG_EVERY` logging (`logger.error` with the carried text — the producer's
  exception context can't cross the queue for `logger.exception`), and the final "N
  skipped this run" total. Nothing increments `errors` from two threads, keeping
  per-frame log-and-skip semantics exact.

- **Batch failures are visible, not swallowed.** On a batch `analyze_batch`
  exception the main loop retries that batch **per-image** to isolate a single bad
  frame (preserving log-and-skip). But a *batch-level* failure — most likely a CUDA
  **OOM** from too large a `CAT_YOLO_BATCH` — is logged **distinctly** and counted
  separately from per-frame decode skips, so a misconfigured batch size that
  silently degrades every batch to batch-1 throughput (the exact starvation this
  spec fixes) is observable to the operator rather than looking like a slow-but-
  healthy sweep. (CUDA OOM can also leave the context degraded so per-image retries
  fail too — another reason to surface it loudly.)

### The store (`compute/collection/store.py`)

- In `__init__`, after `connect`, set `PRAGMA journal_mode=WAL` and
  `PRAGMA synchronous=NORMAL`. WAL persists on the DB file (adds `-wal`/`-shm`
  sidecars) and applies store-wide. Because a single connection under one `Lock`
  serializes all access, the win here is **cheaper commits** (fsync deferred to
  checkpoint), not reader/writer concurrency. On power loss the last committed
  transactions may be lost but the DB is not corrupted.
- **Known consequence on the always-on collector (accepted).** `synchronous=NORMAL`
  weakens `add()`'s file↔row atomicity: `add()` writes the JPEG then commits the
  row, so a power loss can drop the just-committed row while the file survives.
  `_total_bytes` is rebuilt from `SUM(bytes)` over rows at startup, so that orphan
  file is never counted and therefore **never evicted** — a small, non-self-healing
  disk leak (only the last few un-checkpointed frames, only on hard power loss). We
  accept it for a re-runnable frame/verdict store; noted here because it is the one
  hard-to-notice commitment WAL introduces, and the driver was sweep speed, not
  collector durability. A `PRAGMA wal_checkpoint(TRUNCATE)` on clean shutdown bounds
  the exposure — home it in a new `Store.close()` called from the app's existing
  `on_event("shutdown")` hook (`compute/api/app.py`). A code revert also leaves the
  DB in WAL mode; harmless (old code opens WAL transparently), fully undone by a
  one-time `PRAGMA journal_mode=DELETE`.
- Add `write_analysis_batch(rows)`: under one `self._lock`, `executemany` the
  identical `INSERT OR REPLACE INTO analysis … SELECT … WHERE EXISTS (SELECT 1 FROM
  frames WHERE id = ?)` guard, then one `commit()`. Preserves idempotency and the
  "a verdict can't outlive its frame" eviction guard; `write_analysis` stays for
  the windowed path.

### Verdict preservation & validation

**Bit-identical parts.** Prefetch only moves decode to another thread; batched
commit only changes *when* rows are flushed. Neither touches inference input or
math — verdicts are provably unchanged.

**Batching — verdict-preserving, but not provably bit-identical.** Grouping frames
into one `predict()` call runs the *same* model over the *same* pixels, so in
principle the verdict is identical. Two GPU realities make "bit-identical" too
strong, though: (a) cuDNN can select different algorithms / reduction orders by
batch size, so outputs can differ in the last FP bits; (b) if stored frames differ
in dimensions (the clip rectangle is operator-editable and may have changed
mid-collection), ultralytics letterboxes a batch to a *common* shape, so a frame's
preprocessed input can differ from its single-image form.

Risk (b) is **eliminated by construction**: the batcher only groups **contiguous
same-dimension frames** into one call (a one-line shape check as it fills a batch,
flushing early at a dimension boundary), so every batch letterboxes exactly as the
single-image path would — dimension changes are rare (operator clip-rect edits), so
this costs almost nothing. That leaves only (a), last-bit cuDNN noise, which could
flip a detection sitting right at the `conf=0.15` floor — expected to be vanishingly
rare, and what the validation gate measures.

**Validation gate (one-time, before trusting the fast path).** Diff the fast path
against the serial path: pick a representative sample window, run it both batched
and per-image, and report how many `(verdict)` rows differ and their scores. Expect
0, or a handful of frames whose score sits within epsilon of `0.15`. If the count
is material (not conf-floor noise), treat it as a bug in the batched path, not an
accepted cost. Because both runs write the same `(frame_id, 'yolo')` rows
(`INSERT OR REPLACE`), the diagnostic must **snapshot the first run's verdicts**
(dump to a scratch table/file, or write the second run under a throwaway analyzer
id) before the second overwrites them. This is a throwaway diagnostic, not shipped
machinery — it exists to turn the "verdicts don't move" claim from an assertion into
a measurement.

**Resumability** holds because commits are **batch-atomic**: a crash leaves whole
committed batches persisted and the in-flight batch simply un-verdicted, picked up
on the next `iter_unanalyzed` pass. FP16, if enabled, is the one lever expected to
move verdicts and is off by default.

## Resolved decisions

Settled at spec sign-off (each was an open question; all took the recommended
default):

- **`CAT_YOLO_BATCH` default = 8** — safe fit for `yolo11x` @ imgsz 1280 FP32 on
  16 GB VRAM; tunable up via env, especially once FP16 is on.
- **`CAT_YOLO_HALF` off by default** — keeps verdicts unchanged by default (FP16 is
  the one lever expected to move them); the batching/prefetch win alone should hit
  the target since the GPU is starved, not saturated.
- **Verdict guarantee = validated near-zero** — reframe from "bit-identical" to
  "verdict-preserving, confirmed by a one-time batched-vs-serial diff" (see *Verdict
  preservation & validation*); strict bit-identical would gut the batching win for a
  handful of conf-floor frames.
- **WAL / `synchronous=NORMAL` adopted store-wide** — one-line, broadly-beneficial
  cheaper commits; batched commits already do most of the work, so this is the
  smaller secondary lever. The collector commits under it too (accepted).

## Alternatives considered

- **Config-only quick wins** (FP16 + WAL + batched commits, no prefetch/batching).
  ~1.5–2× and a tiny diff, but leaves the GPU at `batch=1` and still starved, so it
  caps short of the target. Its pieces are a strict subset of this design and can
  land first if staging is wanted.
- **Batch the windowed path too.** Rejected — BSUV/MOG2 depend on strict temporal
  order and rolling state; batching would corrupt the background window.
- **Multiple decode threads / process pool.** Unnecessary — one producer decoding
  at ~100 fps stays well ahead of a GPU consuming < ~25 fps; more threads add
  contention for no gain. Revisit only if profiling shows decode as the bottleneck.
