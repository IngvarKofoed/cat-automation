# Split the compute UI into an admin area and a blank user index

Move the entire existing compute SPA (Start, Activity, Buckets, Sweeps, Tuning,
Annotate, Training) from `/` to `/admin`, and serve a new, near-blank
user-friendly page at `/`. The two are **physically separate HTML files** with
their own inline `<style>`, so they share no CSS and can be styled
independently. Only the `/api/*` + `/media/*` backend stays shared. No auth, no
backend behavior change — this is purely a front-door reorganization that
carves out room for the real user-facing dashboard to grow later.

## Key decisions

- **Two separate documents, no shared CSS** (diverges). Today the whole UI is
  one 5,852-line `web/index.html` with all CSS/JS inline. We split it into two
  self-contained files, each owning its own `<style>`. Physical separation is
  what guarantees the isolation the user asked for — no scoping tricks, no
  namespacing.
- **User at `/`, admin at `/admin`** (new). `GET /` serves the blank user page;
  `GET /admin` serves the moved SPA. Admin comes off the root so the user index
  can own it.
- **Admin SPA moves near-verbatim, hash routing intact** (reuses). Its network
  calls are all absolute (`/api/...`, `/media/...`) and its routing is hash-based
  (`#activity`, `#tuning`, …), so served from `/admin` it just becomes
  `/admin#activity` with **no change to the router or any fetch**. Only its
  `<title>` and file location change.
- **Subdirectory layout, explicit routes** (new). Files live at
  `web/user/index.html` and `web/admin/index.html`; two explicit `FileResponse`
  routes serve them, mirroring the current `@app.get("/")` style. The subdirs are
  *organizational* — they group each area's files so the deferred `StaticFiles`
  mount drops cleanly onto one dir when an area goes multi-file. Explicit routes
  serve only the one named file: a new sibling file still needs that mount (a
  Python change), subdir or not. We do *not* pull in Starlette `StaticFiles`
  while the user page is a single blank file.
- **Backend and API unchanged** (reuses). Every `/api/*` and `/media/*` route,
  the store, and all page behavior are untouched. The split is HTML/CSS/JS only.
- **No shared design-token stylesheet** (diverges, deliberately rejected). The
  admin UI's two-tier design-token layer (changelog 43/65/66) stays *inside* the
  admin document. We explicitly do **not** extract it into a stylesheet both
  areas import — that would re-couple their CSS and defeat "style them
  independently." Recorded so a future DRY pass doesn't undo it.

## Goals

- A blank user index at `/` whose styling inherits nothing from the admin CSS.
- Every current page reachable and behaviorally unchanged under `/admin`.
- Two independent CSS worlds that can diverge freely.
- Zero change to `/api/*`, the data model, auth posture, or admin page behavior.

## Non-goals

- Designing or populating the user area — it is a placeholder this round.
- Any authentication or user management (still a trusted-LAN prototype).
- Refactoring the admin SPA's internals; it relocates as-is.
- Introducing a build step, bundler, or `StaticFiles` mount.

## Design

### File moves

- `compute/api/web/index.html` → `compute/api/web/admin/index.html`
  (the current SPA, content unchanged except its `<title>`, e.g.
  "Cat Automation — Admin").
- New `compute/api/web/user/index.html` — the blank user page (below).

### Serving (`compute/api/app.py`)

Replace the single index route with two. Today:

```python
_WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"

@app.get("/")
def index():
    if not _INDEX_HTML.is_file():
        raise HTTPException(status_code=404, detail="browse UI not built")
    return FileResponse(_INDEX_HTML, media_type="text/html")
```

After: constants `_USER_HTML = _WEB_DIR / "user" / "index.html"` and
`_ADMIN_HTML = _WEB_DIR / "admin" / "index.html"`, with `GET /` serving
`_USER_HTML` and a new `GET /admin` serving `_ADMIN_HTML`. Each keeps the same
"404 if the file isn't there" guard so a missing frontend is an obvious
not-found, not a crash. Nothing else in `app.py` changes.

### Why the admin move is safe

The SPA is fully self-contained (inline CSS/JS, no CDN, no relative asset refs),
every fetch is an absolute `/api/...` path, and routing is `location.hash`-based.
Served from `/admin`, `/admin#activity` resolves the same route the old
`/#activity` did, and `location.hash = 'buckets'` mutates only the fragment,
leaving the path on `/admin`. So the relocation needs no JS edits.

### The blank user page

A minimal self-contained document: its own `<style>` block (empty or a bare
reset), a `<title>` ("Cat Automation"), and a placeholder marking it as the
future user dashboard. It deliberately shares no markup or CSS with admin — a
clean slate for the real occupancy/timeline UI described in CONCEPT.md.

It carries **one discreet link to `/admin`** so the workbench is reachable in the
browser while the user page is blank (otherwise the page is a dead end). The link
names only the `/admin` path — no admin CSS crosses over — and is trivial to
remove once the real dashboard lands. The reverse (an admin → `/` link) is
deferred: admin keeps its current nav unchanged this round, and a home link back
to the user area waits until that area is a real destination.

Legacy admin-hash bookmarks (e.g. `/#tuning`) are **not** redirected: `/` now
serves the blank page, and an old fragment is simply ignored. Keeping the
clean-slate page free of admin's route names is worth more than preserving a
single dev's bookmarks — the broken-bookmark surprise is a known, chosen cost,
not a bug. Re-bookmark to `/admin`.

### Launcher URL

`compute.sh` and `compute.ps1` both print the root URL as the "browse UI"
(`http://localhost:${PORT}`), which now lands on the blank page. Update the
printed line so the operator is pointed at the workbench — keep the base line
and add an explicit `…/admin` hint alongside it.

## Alternatives considered

- **Shared design-token stylesheet, both areas import it.** The DRY instinct —
  and exactly what the "style independently" requirement forbids. Rejected; kept
  in Key decisions as a standing "do not re-introduce."
- **StaticFiles mount per area.** Auto-serves future sibling assets with no
  Python change, but adds machinery (mounts, trailing-slash redirects) unneeded
  while the user page is one blank file. It's the trivial upgrade the day the
  user area becomes multi-file; not now.
- **Keep one file, scope admin CSS under a prefix / shadow DOM.** Preserves a
  single document but leaves the design tokens global and the isolation fragile —
  the opposite of the physical separation the requirement wants.
