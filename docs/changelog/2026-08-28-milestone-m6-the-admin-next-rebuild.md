# Milestone M6 — The admin-next rebuild

<!-- milestone M6: entries 121–221 -->

**M6 — The admin-next rebuild** (121–221). The workbench was rebuilt page by page beside the old one,
then flipped: `/admin` serves the rebuild and the old console is deleted, with buckets (superseded by
the tuning calendar) and corruption review dropped by decision — their endpoints survive, with no UI.
Six pages: Start, Motion tuning (4-week calendar picker, sweeps, MOG2 re-runs, day/night scorecards,
missed-visit review attributing each miss to a knob), Frame review, Annotation, Model building,
Activity. Conventions later entries assume: a sweep's watermark re-seeds to the frame horizon on every
`start`; `yolo-serial` is renamed only in presentation (it is the stored `analysis.analyzer` value);
`recv_ts` monotonic with `id` is assumed, never enforced; and an unmeasured window must never read as
a clean one.
