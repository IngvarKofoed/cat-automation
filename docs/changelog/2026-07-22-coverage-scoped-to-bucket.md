# Oracle coverage readout is scoped to the selected bucket

<!-- entry 36 -->

Motion-view oracle coverage is scoped to the selected bucket, not whole-store. New GET
/api/analysis/coverage returns per-oracle {analyzed, present} against the window's frame total,
so "X/N analyzed · P present (in this bucket)" and the enqueue confirmation ("enqueued over
bucket …") make it clear what a scoped sweep will actually cover — the enqueue was already
bucket-scoped; only the display lagged.
