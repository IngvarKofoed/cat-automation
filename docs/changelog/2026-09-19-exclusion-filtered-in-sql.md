# Exclusion is filtered in SQL on both crop readers and the pre-check

<!-- entry 332 -->

Filtered in SQL on `labeled_crops` AND `count_identified_crops`, the pair that must
agree. It is the FIRST build parameter that can reduce the CAT count, so the pre-check
applies it too — the cap's "only reduces crops per cat" premise does not hold here, and
the two-cat floor would otherwise pass on cats the build then drops. The message names
the exclusion: "not enough labelled data" misreads when it was merely deselected.
