# Events carry a subject beside identity

<!-- entry 89 -->

Activity events now carry a `subject` ("what is it": cat / person / bird / unrecognized / motion_only)
beside `identity` ("which cat"), so a false-motion trigger, a human, and an unnameable subject each get
a distinct chip instead of all collapsing to one blank "no chip" card. `yolo-serial` broadened to detect
person(0)+bird(14)+cat(15); verdict/score stay CAT-ONLY so the motion-gate scorecard's "verdict=1 ⇒ cat"
contract is unchanged; detail boxes gain a 6th class element (legacy 5-elem rows read as cat).
The floor splitting `unrecognized` (cat-scale motion YOLO couldn't name — worth a look) from `motion_only`
(below it — likely noise) is LEARNED from labelled cat visits' motion, stamped on `model_versions.metrics`
at gallery-build; a conservative default applies pre-calibration.
A confident NAMED gallery match promotes the subject to `cat` even below the 0.3 detection floor, so a
low-confidence resident is never hidden behind a motion chip (an unknown/far match is not promoted —
phantom-safe). Read-time + additive: event clustering, the identify path, and the batched `yolo` oracle
are untouched. Spec: docs/specs/2026-07-22-event-subject-classification.md.
