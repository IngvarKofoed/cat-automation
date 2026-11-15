# Changelog split into one file per entry under docs/changelog/

The changelog is now `docs/changelog/`, one `YYYY-MM-DD-<slug>.md` file per change and no
entry numbers. All 612 numbered entries and the six milestone summaries were split out of
`docs/CHANGELOG.md` and `docs/CHANGELOG-archive.md`, which are gone from the tree.
A session orients by listing the folder, reading the newest ten, and grepping it.

## Detail
- Dates on the 618 backfilled files are SYNTHETIC: five entries per day, newest-first from
  2026-11-14 back to 2026-07-15. They give order and rough vintage only, so a backfilled entry
  citing a dated spec can disagree with its own filename.
- Each carries `<!-- entry N -->` (milestones: `<!-- milestone M1: entries 1–15 -->`), so the
  `entry N` / `CHANGELOG N` citations in code, tests and specs resolve by grep. Text was copied
  verbatim minus the number, and checked against both sources before they were removed.
- Why now: an archive pass had already happened (`2026-10-01-changelog-archive-first-move`)
  and the live file was back to 2,347 lines; one shared file also cannot take parallel work.
- The milestone summaries became entry files rather than being dropped: they carry the
  conventions later entries assume. No digest or curated summary file replaces them.
- The originals stay retrievable with `git show <commit before this one>:docs/CHANGELOG.md`.
