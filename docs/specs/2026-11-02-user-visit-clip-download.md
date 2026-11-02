# Download a visit as an MP4 clip

The user app's playback modal shows a visit as a run of JPEG frames, and there is no way to
keep one. This adds a **Download clip** button to that modal: the compute PC stitches the
visit's stored frames into an H.264 MP4, timed so the clip runs as long as the visit did, and
the browser downloads it. The target is an iPhone, where an H.264 MP4 plays in Files and Photos
with nothing installed. The download lands in Files › Downloads, and the iPhone's own *Share →
Save Video* moves it into Photos.

The clip is made **on demand and never stored**. The frames already live in the frame store,
so a clip is a view of them. Keeping MP4s would add a second media store outside the byte cap,
with nothing to evict it.

## Outcome

**What you get:**

- A **⬇** button in the user app's visit player that downloads the visit as an MP4. On the
  iPhone it lands in Files › Downloads, and *Share → Save Video* puts it in Photos.
- The clip holds every stored frame of the visit, plays at real speed, and lasts as long as the
  visit did. It plays on the iPhone with nothing installed.
- When a clip can't be made, the player says why: the frames have aged out, another clip is
  being made, or the compute PC is missing the encoder package.

**How to verify:**

- Open a visit on the iPhone in Safari and tap ⬇. The icon pulses, and the line below reads
  *Making clip…*, then *Clip ready (N MB) — tap ⬇ to save*. Tap ⬇ and confirm the download.
  The clip opens from Files › Downloads, and *Share → Save Video* puts it in Photos. Its length
  matches the time from the visit's first frame to its last.
- Repeat from the home-screen icon. Either it saves the same way, or it doesn't, and that is
  reported as a known limit ("use Safari").
- Tap ⬇, hop to another visit before the clip is ready, and tap ⬇ there. The new clip starts
  at once, with no "another clip" message. The old visit's clip is never offered.
- On the compute PC's real store, make a clip of the longest visit available. It arrives
  inside 45 s. While it encodes, the stored-frame count keeps rising. With the alarm switch
  On, the alarm card's pass readout keeps ticking too.
- Tap ⬇ on two devices at once. The second one says "Another clip is still being made — try
  again in a moment." In a desktop browser, the clip downloads as `catdoor-….mp4`.

## Key decisions

- **Encoded on the compute PC, on request** (new). `GET /api/clip?start_id=&end_id=` reads the
  span's frames, encodes them into a temporary file, streams it back as a download and deletes
  it. There is no clip table and no cache. Asking twice encodes twice, which costs seconds
  and saves a second store with its own eviction rules.
- **`imageio-ffmpeg` is the encoder** (new, dependency). The pip package ships a static
  `ffmpeg` binary that includes libx264 for Windows, Linux and macOS. It goes into
  `compute/requirements.txt`, so nobody installs ffmpeg by hand. It runs as a subprocess
  (`imageio_ffmpeg.get_ffmpeg_exe()`). The binary is GPL, which is fine for a household
  install that is never distributed.
  - **Rollout step:** `compute.ps1` and `compute.sh` install `requirements.txt` only when they
    create the venv, so the existing compute PC needs one manual
    `.venv-compute\Scripts\python -m pip install -r compute\requirements.txt`.
  - **Rejected: OpenCV's `VideoWriter`.** It is already installed, but its H.264 support
    depends on the OS backend (none in the Linux wheels). It also writes at a constant rate,
    so it can't carry each frame's own timing.
- **Output pinned to what an iPhone plays** (new):
  - H.264 at `yuv420p`, **limited range**, with `+faststart` and no audio track.
  - Limited range is forced with `out_range=tv` on the scale filter. `-pix_fmt yuv420p`
    alone does not do it: the JPEGs' full-range flag survives the conversion, and the stream
    still reads `yuvj420p(pc)` (measured on the bundled ffmpeg 7.1).
  - Width and height are rounded down to even numbers, which 4:2:0 requires.
  - `+faststart` puts the index at the front, so Safari can play the file before it has all
    of it.
- **Every stored frame in the span, at real speed** (extends). The clip uses the same
  `[start_id, end_id]` span the player samples (`/api/frames/sample`, ≤ 240 frames). It takes
  every frame instead of a sample, and times each one from `recv_ts`. A 40 s visit gives a
  40 s clip at the capture rate (~10 fps, entry 275). Where frames are missing, the previous
  frame is held for the gap. Gaps come from Collecting/Running storing motion frames only, or
  from frames that have evicted. So the clip shows what happened, not a sped-up summary.
- **Long visits are thinned, not refused** (new). Above `_CLIP_MAX_FRAMES` = 3000 (~5 min
  at 10 fps), the clip keeps every *k*-th frame, *k* = ⌈n / 3000⌉. It is timed off the kept
  frames' `recv_ts`, so it still lasts as long as the visit did, at a lower frame rate. The
  cap bounds the encode time, which has to finish before the phone's request gives up (see
  *Encoding*). The span itself is still bounded by `_validate_visit_span`'s 10 000 ids (reuses:
  both bounds required), like the other per-visit routes the phone calls. In Tuning that is
  about 16 minutes of visit. A wider visit is refused with "This visit is too long to save as
  one clip.", not the validator's engineering text.
- **One encode at a time, off the store lock, on bounded threads** (extends). The span's rows
  come from one id-range read, then the store lock is released before any file is checked
  or ffmpeg starts. `ffmpeg` runs at below-normal priority, with its decoder, filters and
  encoder each held to a thread or two (see *Encoding*). A second request while an encode
  runs gets `409`. The collector, the detection worker and the alarm's 1 s tick share this machine.
  A household tapping Download twice must not starve them. That is the starvation class of
  entries 102–105, on the CPU this time instead of the write lock.
- **An encode whose phone has gone is killed** (new). The route is `async`. It runs the read
  and the encode off the event loop and watches `request.is_disconnected()` while ffmpeg
  runs. If the phone leaves the visit, the fetch is aborted, and the route kills ffmpeg,
  deletes the temporary files and frees the lock. That only happens at the route's next
  disconnect check, so a request that finds the lock held waits up to two check intervals
  (1 s) before answering `409`. Browsing on, then tapping ⬇ on the next visit, therefore
  never meets "another clip is still being made" about a clip nobody wants,
  and no CPU is spent on it.
- **A two-tap icon in the header, with its words below** (extends). A **⬇** icon sits beside
  the close ×.
  - **First tap:** fetches the clip. The icon pulses, and the footer's message line reads
    *Making clip…*.
  - **Ready:** the icon turns into a real `<a download>` link to the fetched file, and the line
    reads *Clip ready (4.2 MB) — tap ⬇ to save*.
  - **Second tap:** saves it. The second tap is a fresh user gesture, so iOS never treats the
    save as an unprompted download after a several-second wait.
  - **Why words below:** the header has no room for them on a phone. The time, the cat's name
    and the × already fill it.
  - **Leaving the visit** drops the pending clip, by Prev/Next, a swipe or closing the modal.
    A clip must never be offered on a visit it isn't of (entries 228/289).
- **Download, not the share sheet** (new). The share sheet would put *Save Video* one tap
  away, but `navigator.share` exists only on HTTPS pages, and the compute PC serves the user
  app over plain HTTP. Moving the app to HTTPS is its own spec: it needs a certificate each
  iPhone trusts, and the origin change means re-adding the home-screen icon. How iOS
  handles a download inside the home-screen app is checked on the phone. If it can't save
  there, the documented answer is to open the app in Safari.

## Goals

- Save one visit from the user app as a video that plays on an iPhone.
- The clip shows the whole visit at the speed it happened.
- Making a clip never slows the door: the collector, the workers and the alarm keep their time.

## Non-goals

- **Sound.** Nothing records audio, and the alarm is not in the clip.
- **The detection box.** The clip is plain camera frames. The player's overlay is a diagnostic.
- **Trimming, joining several visits, or a time range.** One visit, whole.
- **Storing or listing clips** on the compute PC.
- **The admin workbench.** User app only. The two dashboards share no JS (M4), and admin has
  no call for it yet.
- **The share sheet, or saving straight into Photos.** Both need the app on HTTPS, which is a
  separate spec.

## Design

### The endpoint

`GET /api/clip?start_id=<int>&end_id=<int>` answers `200` with `Content-Type: video/mp4`, and
`Content-Disposition: attachment; filename="catdoor-YYYY-MM-DD-HHMMSS.mp4"`. The name is
built from the first included frame's `recv_ts` in the compute PC's local time, the same
household clock the feed shows. `Cache-Control: no-store`.

The route is `async`, so it can watch for the phone leaving (see *Encoding*). The store read,
the file checks and the ffmpeg wait all run off the event loop (`run_in_threadpool` /
`asyncio.to_thread`), so none of them blocks the rest of the API.

Failures answer with a `detail` sentence the phone shows as-is:

| Code | When | Detail |
|---|---|---|
| 400 | bad span order (`_validate_bounds`) | its existing wording; the phone never sends one |
| 400 | span wider than `_MAX_VISIT_SPAN` (checked by this route first) | "This visit is too long to save as one clip." |
| 404 | no frame file in the span exists any more | "This visit's frames have aged out." |
| 409 | another clip is still being made after a 1 s wait for the lock | "Another clip is still being made — try again in a moment." |
| 503 | `imageio_ffmpeg` won't import, or `get_ffmpeg_exe()` raises | "The compute PC can't make videos yet — run its pip install of compute/requirements.txt." |
| 502 | ffmpeg exited non-zero, or ran past the timeout | "Couldn't make the clip" plus the tail of ffmpeg's stderr |

`503` is the "older build / missing package" state of entries 164/173/183/365. It names the
missing piece, so a phone pointed at a compute PC that hasn't run its venv install doesn't
read as "clips don't work".

### Reading the frames

A new store read returns `(id, recv_ts, absolute path)` for every frame in `[start_id,
end_id]`, id ASC, in one indexed range query. It resolves paths the way `path_for` does,
but for the whole span at once, not one lock round-trip per id. Like `path_for`, it leaves
file existence to the caller.

Only after the store lock is released does the route stat the files. Up to 10 000 stats
under the lock would stall the collector. Rows whose file is missing are dropped. If none
remain, the route returns 404. A file that evicts after this check fails the encode with a
502, which is rare: eviction takes the oldest frames first.

### Timing and thinning

After thinning, each kept frame's duration is the next kept frame's `recv_ts` minus its own.
The last frame gets the median of the others, or 0.1 s for a single-frame visit.

Each duration is clamped to [10 ms, *k* × 2 s], where *k* is the thinning step (1 when
nothing is thinned). The reason for the clamp: `recv_ts` rising with `id` is assumed, not
enforced (entry 278), and a backwards step must not produce a negative or absurd duration.
The reason for the bound: no gap between consecutive frames of one visit exceeds the visit
gap (`_VISIT_GAP_MS`, 2 s), so no gap between kept frames exceeds *k* times that.

ffmpeg gets this timing from a **concat-demuxer list** with a `duration` line per file:
- `-safe 0`, with Windows paths quoted for the list format;
- `option framerate 1000` on every entry;
- the last file listed twice, since the demuxer ignores the final entry's `duration`;
- `-fps_mode vfr` (`-vsync vfr` on older builds) on the output;
- `-x264-params fps=<the clip's real average frame rate>`.

Each of the last three flags matters:
- **`framerate 1000`:** without it, each JPEG keeps the image reader's default 25 fps, so
  every timestamp snaps to a 40 ms grid. `vfr` then drops the frames that land in the same
  slot. A 1 ms grid keeps every frame at its own time.
- **`-fps_mode vfr`:** MP4 output otherwise defaults to a constant frame rate, which
  duplicates frames to fill each duration. The encoded frame count would then grow with visit
  length and not with kept frames, and `_CLIP_MAX_FRAMES` would no longer bound the encode. A
  fixed-rate pipe of repeated frames is rejected for the same reason.
- **`fps=` for x264:** the 1000 fps input otherwise makes x264 compute the H.264 level from
  1000 fps and label a 720p clip High@L6.0, beyond what an iPhone accepts. The real average
  rate brings it back to about L2–3, and no timestamp moves.

All three were measured on the bundled ffmpeg 7.1.

### Encoding

- **Output settings:** `libx264`, `-preset veryfast`, `-crf 23`, `-pix_fmt yuv420p`, with
  `out_range=tv` on the scale filter. Also `-movflags +faststart`, `-an`, `-fps_mode vfr`,
  and `-x264-params fps=<average rate>`.
- **CPU bound:**
  - `-threads 1` before `-i`, for the JPEG decoder;
  - `-filter_threads 1`;
  - `-threads 2` on the output, for x264;
  - the process started at below-normal priority (`BELOW_NORMAL_PRIORITY_CLASS` on
    Windows, `nice` elsewhere).

  An output `-threads` alone limits only the encoder. The decoder and the scale filter
  would still take every core.
- **Size:** the output is the first kept frame's size, rounded down to even. Every frame is
  scaled to it, so a mid-visit change of the edge's clip rectangle cannot fail the encode.
- **Where it's written:** a temporary file in a fresh temporary directory, returned with a
  background task that deletes it once sent.
- **Timeout:** the subprocess has a 45 s timeout. On expiry it is killed and the route
  answers 502. The timeout sits below the phone's own give-up, so the 502 still reaches
  the phone, and the lock is not held for a clip nobody will receive.
- **The lock:** a module-level non-blocking lock is held from the start of the read to the
  end of the encode, and released on every exit path. It is not held while the file is
  being sent.
- **Disconnects:** ffmpeg runs as a `Popen`. While it runs, the route checks
  `await request.is_disconnected()` about every 0.5 s. When the phone has gone (aborted
  fetch, closed modal, backgrounded app), the route kills ffmpeg, deletes the temporary
  directory, releases the lock and returns nothing. A new request that finds the lock held
  waits up to two check intervals (1 s) for it, so the phone's next tap after a hop starts a
  fresh encode. An encode that is genuinely still running still gets the `409`.

The phone is why the frame cap exists. iOS Safari gives up on a request whose response hasn't
started after roughly a minute. With `+faststart` nothing is sent until the encode is done. So
the worst case, 3000 frames, must encode inside the 45 s timeout on the compute PC. This is
unmeasured: the dev box has neither the real frame size nor the real CPU. It is the first
thing to check there.

### The button (user app)

The control is an icon in the playback modal's header, beside the close **×**
(`user/index.html:1040`). The header holds no text for it, and the footer row is already full
on a phone (entries 250/515). Its words go on the footer's message line (`playerFlagMsg`).

| State | Icon | `aria-label` | Message line |
|---|---|---|---|
| idle | ⬇ button | "Download clip" | unchanged |
| making | ⬇ pulsing, disabled | "Making clip" | *Making clip…* |
| ready | ⬇ in the accent colour, an `<a download>` | "Save clip, 4.2 MB" | *Clip ready (4.2 MB) — tap ⬇ to save* |

- **Tap (idle):** the page fetches `/api/clip` for the visit's `start_id`/`end_id` with an
  `AbortController`.
- **The pulse:** it reuses the user app's `savepulse` keyframes. The existing rule,
  `.saving .pulse` (`user/index.html:930`), is scoped to the saving pill, so the icon needs a
  rule of its own (entries 287/471's trap).
- **Ready:** the response becomes a blob, and the icon becomes `<a href=URL.createObjectURL(blob)
  download="<name from Content-Disposition>">`. Tapping it hands the file to the browser's
  download.
  - **After the download:** the clip stays ready, so tapping again saves it again with no new
    encode. Nothing reports that a download has finished, and revoking the object URL right
    after the tap can cancel it in Safari. So the URL is revoked only by the leave-visit reset
    below.
- **The message line is shared** with the flag and confirm readouts, and their writes can
  replace the clip's sentence. The icon's state, colour and `aria-label` still carry it, so
  nothing is lost but the words.
- **Error:** the response's `detail` goes to the message line, and the icon returns to idle.
- **No response at all** (the compute PC unreachable, the phone giving up, the app
  backgrounded): any fetch rejection other than our own abort prints "Couldn't reach the
  compute PC, or the clip took too long." on the same line, and the button returns to idle.
  An `AbortError` from leaving the visit stays silent.
- **Leaving the visit:** a hop, a swipe or closing the modal aborts the fetch, revokes any
  object URL, resets the icon and clears the clip's sentence. The abort is also what tells
  the server to stop the encode. The state lives on the visit that was on screen when tapped.
  Only that visit's repaint shows it, so a clip is never offered under a different visit.

The icon is hidden for a visit with no span (none today, but the feed's contract allows
nulls). It is not hidden for visits whose frames may have aged out: the 404 explains that
better than a missing button would.

### Docs

- **CONCEPT, §6:** the dashboard "shows status, events, and stored snapshots — not a live
  video feed". Add that a visit can be downloaded as a video.
- **CONCEPT, *Out of scope*:** "Live video access and clip capture/replay of door events"
  becomes live video only. Downloading one visit is now in scope.
- **ARCHITECTURE, Components table:** the *Dashboard + API* row mentions the clip download.
- **ARCHITECTURE, *Storage*:** "Media (crops/clips) is stored on the compute tier's
  filesystem" becomes crops only. Clips are made on demand and never stored.
- **ARCHITECTURE, *Recommended tech stack*:** gains the `imageio-ffmpeg` row.
- **ARCHITECTURE, principle 4 and *Security & privacy*:** these say imagery "never leaves
  the LAN" and "no cloud storage of video". Both become "the system never sends imagery off
  the LAN". A clip the household saves to a phone is theirs, and it may reach iCloud Photos,
  AirDrop or Messages.

## Alternatives considered

- **Encode on the iPhone** (WebCodecs + an MP4 muxer). Rejected:
  - it needs a recent iOS and a library loaded from the internet, when the user app loads
    nothing external;
  - the phone fetches every frame and encodes on battery;
  - none of it can be exercised from the dev box.
- **OpenCV `VideoWriter`** (no new dependency). Rejected:
  - it writes at a constant rate, so it can't carry each frame's own timing;
  - its H.264 support depends on the OS backend, and the Linux wheels have none.
- **The player's ≤ 240-frame sample at ~5 fps.** Rejected: a long visit comes out choppy and
  slower than real time. The clip is for keeping, not for matching the player's preview.
- **An async job with a "ready" link** (the TrainingManager queue). Rejected: a normal visit
  encodes in seconds. The frame cap keeps the worst case inside one request, so a queue
  would add polling and state for no gain.
- **The share sheet** (*Save Video* straight into Photos). Chosen first, then dropped:
  `navigator.share` needs HTTPS, and the app is served over plain HTTP. It returns with an
  HTTPS spec, if one is ever written.
- **Letting an abandoned encode finish.** Simpler, but the next tap would read "another clip
  is still being made" for up to 45 s, about a clip nobody wanted, and the CPU would be spent
  for nothing.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.5.** One thread through four places that share one contract: the
  store's span read (`store.py`), the `async` route with its ffmpeg subprocess (`app.py`),
  `requirements.txt`, and the icon's three states (`user/index.html`). The risk is in the
  interpretation: per-frame VFR timing, Windows path quoting, the kill-on-disconnect path,
  and per-visit UI state. That is Opus work, and too small to split.

## Amendments

- **2026-11-02.** The concat list gains `option framerate 1000` per entry and
  `-x264-params fps=<average rate>`. As first written, the list snapped every frame to
  ffmpeg's default 25 fps grid, and `vfr` dropped the frames that collided. The 1 ms grid
  fixes that, and the `fps` hint keeps x264's H.264 level iPhone-sized.
- **2026-11-02.** Limited range is forced with `out_range=tv`. The spec claimed
  `-pix_fmt yuv420p` did it, but the output measured `yuvj420p(pc)`.
- **2026-11-02.** A request that finds the clip lock held waits up to 1 s before its `409`.
  With an immediate `409`, a tap right after a hop hit the abandoned encode before its next
  disconnect check had freed the lock.
