# require_gallery queue filter, on by default

<!-- entry 415 -->

New `require_gallery` queue filter, ON by default: keep only visits holding a crop a
quality-filtered build would enrol. It does NOT subsume the frame floor and both are
kept — a lone frame's area ratio is 1.0 by construction, so a single-frame visit at
≥60% grades `gallery` on a comparison with itself, which this filter SHOWS and the
floor hides. Only the pair means "would contribute a crop worth enrolling".
