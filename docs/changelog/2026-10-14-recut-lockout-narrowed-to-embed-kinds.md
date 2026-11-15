# Recut lockout narrowed to kinds that embed crops

<!-- entry 456 -->

The recut/training lockout is narrowed to the kinds that actually embed crops
(`feasibility`, `gallery-build`), mirroring `_refuse_during_recut`'s own narrowing.
`identify`/`visit-identify` read frames and write identities, never touching
`dataset_items` files, so a whole-store Identify pass was refusing a re-cut for no
reason. A QUEUED embed kind still blocks even when nothing is running yet.
