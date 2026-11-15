# GET /api/label/enrollable reports per-cat crops at the build grades

<!-- entry 335 -->

New `GET /api/label/enrollable` returns, per ACTIVE cat, crops + `label_commits` at the
requested grades — pre-cap, which is also what a cap is picked against. Counts follow the
Build grades: a cat with 50 labels may have 17 at gallery grade, so 50 is the wrong
number to decide on. `label_commits` is named for what it counts, not the visits it
proxies (~12% high). Retired cats are omitted — unticking implies ticking would enrol.
