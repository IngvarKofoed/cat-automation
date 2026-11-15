# reset_watermark is epoch-guarded and a no-op on a never-enabled worker

<!-- entry 144 -->

`reset_watermark` is epoch-guarded and no longer consumes the first-enable seed. `/api/clear` does NOT stop the
workers, so a tick in flight used to write its pre-wipe watermark back over the reset — undoing it and stranding
the worker exactly as the reset exists to prevent. It is also now a NO-OP on a never-enabled worker: writing the
key there consumed `_seed_horizon` permanently (it is re-derived as "no persisted key"), so clear → collect a day →
first enable would back-sweep the whole store — the hours-long GPU hold the seed exists to prevent.
