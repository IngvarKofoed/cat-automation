# Report with unavailable visit scoring keeps its crop-level verdict

<!-- entry 328 -->

A report whose visit scoring was UNAVAILABLE keeps its crop-level verdict. `demoted`
keyed on the visit section rendering any HTML, but it also renders an explanatory
banner — so an unavailable run suppressed the only verdict it had AND pointed the
reader at "the visit-level number above", which was never computed.
