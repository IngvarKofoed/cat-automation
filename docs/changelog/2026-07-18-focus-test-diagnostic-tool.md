# Standalone focus diagnostic separates lens fault from code fault

<!-- entry 15 -->

Added `edge/tools/focus_test.py`, a standalone Picamera2 focus diagnostic (run with the
edge server stopped). It isolates whether the Module-3 lens physically moves from whether
the edge's best-effort, error-swallowing focus path silently failed — the two look
identical from the UI, so a hardware fault couldn't be told from a code fault otherwise.
