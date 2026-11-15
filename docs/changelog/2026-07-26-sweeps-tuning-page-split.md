# Workbench split into sweeps and tuning pages with oracle matrix

<!-- entry 55 -->

Motion workbench split into two hash-routed pages: #sweeps (oracle sweeps + job queue) and
#tuning (MOG2 tuning + timeline + inbox); #motion redirects to #sweeps.
On Tuning, a coverage-driven multi-select drives a visit-recall matrix comparing the gate
against multiple trusted oracles side-by-side (client-side fan-out, no backend change).
Bucket scope mirrors across both pages. Separates producing oracle verdicts from evaluating
the gate, enabling the YOLO vs YOLO-serial vs BSUV comparison (entry 54).
