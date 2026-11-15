# CSI corruption fixed by a video configuration with four buffers

<!-- entry 53 -->

CSI capture fixed the green-stripe / purple corrupted frames on the Module 3 (IMX708):
the backend used a full-res `create_still_configuration` (a SINGLE buffer, meant for one-shot
stills) driven as a continuous ~5 fps loop, so libcamera handed back half-filled buffers.
Now a `create_video_configuration` at 2304x1296 (2x2-binned, lower-noise at night) with
buffer_count=4. Also likely quiets the benign `PDAF data in unsupported format` log spam,
which rides the full-res sensor mode; that error was never the corruption and is harmless.
