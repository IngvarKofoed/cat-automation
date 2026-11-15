# Oracle coverage is forward-only and idles under motion-only capture

<!-- entry 142 -->

Oracle coverage is forward-only by construction: first enable seeds the watermark to the frame horizon (a full back-sweep
would hold the GPU for hours), and it fills only MISSING verdicts. So earlier days still need a manual sweep, and a
broadened detector still needs a manual `reanalyze`. It also idles under motion-only capture, where the non-motion
frames a gate miss lives in are never stored.
