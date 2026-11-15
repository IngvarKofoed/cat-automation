# Visit threshold is calibrated on cross-visit same-cat pairs

<!-- entry 318 -->

The visit threshold is calibrated on CROSS-VISIT same-cat pairs, never the crop-level
one. `_best_threshold` over all same-cat pairs is dominated by same-visit
near-duplicates at near-zero distance: measured 0.00063 vs 0.99934 on a real-shaped
fixture (1587x), at which every visit is DECLINED and the report reads 100% unknown.
Data-dependent — with separable cats the two coincide — so it cannot be assumed benign.
