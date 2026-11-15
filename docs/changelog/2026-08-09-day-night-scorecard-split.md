# Scorecard splits per visit into day and night

<!-- entry 123 -->

Day/night tuning-scorecard split (admin-next P2): new `compute/analysis/suntimes.py` computes
sunrise/sunset offline via `astral` (new compute dep, lazy-imported). `Store.gate_scorecard` gains an
optional per-VISIT day/night split (bucketed by each visit's FIRST present frame; warm-up applied
once); `GET /api/tuning/compare?split=1` splits when a location is set, else reports it unavailable
(never guesses a boundary). The classifier keys day/night off the nearest sun EVENT, robust at any
longitude — a single UTC date's (sunrise,sunset) pair mis-classifies far-from-Copenhagen longitudes.
