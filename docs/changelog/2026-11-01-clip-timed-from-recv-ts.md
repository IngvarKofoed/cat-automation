# Clip is timed from recv_ts via concat-demuxer durations

<!-- entry 543 -->

A clip is timed off `recv_ts` through a concat-demuxer list, one `duration` per frame, so gaps
(motion-only storage, eviction) hold the previous frame instead of speeding the clip up.
Above 3000 frames every k-th is kept: bounded encode, still full length. The encode must
finish inside the phone's ~1-min give-up, so a 45 s timeout kills it and answers 502.
