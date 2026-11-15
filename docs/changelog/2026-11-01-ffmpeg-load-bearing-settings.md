# Three measured ffmpeg settings are load-bearing

<!-- entry 544 -->

Three ffmpeg settings are load-bearing, all MEASURED. Each list entry carries `option framerate
1000`: the 25 fps default snaps timestamps to a 40 ms grid and `-fps_mode vfr` DROPS frames
sharing a slot. That base reads to x264 as 1000 fps (High@L6.0 at 720p, a level phones may
refuse), so it is told `fps=<the clip's real average>`. And `out_range=tv` in the scale: with
`-pix_fmt yuv420p` alone the JPEGs' full-range flag survives, reading `yuvj420p(pc)`.
