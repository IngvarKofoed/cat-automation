# Grade tally is suppressed for not_cat and ignored

<!-- entry 248 -->

The grade tally is suppressed for `not_cat` and `ignored`, which are committed crop-less
(quality/bbox/crop_path NULL), so a tally there read "8 ungraded" about crops that were
never written. Keyed on the label KIND, not on "every grade is null": for a real cat
labelled before grading existed, "N ungraded" is true and worth showing.
