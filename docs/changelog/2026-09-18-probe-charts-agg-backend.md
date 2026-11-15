# Probe charts pin matplotlib's Agg backend

<!-- entry 324 -->

Probe charts now pin matplotlib's Agg backend (one `_plt()` accessor). They render in
TrainingManager's WORKER thread, where matplotlib warns a GUI backend "will likely
fail"; Agg is correct by definition since every figure goes straight to a base64 PNG.
