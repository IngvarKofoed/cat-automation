# Gallery build takes an optional per-cat cap

<!-- entry 231 -->

Gallery build takes an optional PER-CAT CAP (`max_per_cat`, blank = uncapped), because
the door makes the dataset permanently skewed — a resident crosses many times a day,
a neighbour visits occasionally — and no amount of further collection fixes a ratio the
door itself sets. 1-NN matching makes imbalance milder than for a classifier but not
harmless: the dominant cat's vectors blanket more of the embedding space, and the
suggested threshold is calibrated from a distribution its pairs dominate.
