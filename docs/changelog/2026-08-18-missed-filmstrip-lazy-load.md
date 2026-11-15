# Missed-visit filmstrips load only when scrolled into view

<!-- entry 168 -->

admin-next tuning: each missed visit's filmstrip loads only when its row scrolls into view
(`IntersectionObserver`), which is what makes the uncapped list affordable — an eager panel
would fire one `/api/frames/sample` per miss on open. The strip covers the visit's id SPAN, so
it is labelled against the span width, not the detection count: `sample_frames` decimates by a
STRIDE and returns fewer frames than the `count` asked for, so "did it sample?" can't be tested
against that count.
