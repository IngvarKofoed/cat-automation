# Training Run button no longer sticks during submit

<!-- entry 64 -->

Training-page Run button no longer gets stuck. It's driven by a `trainSubmitting` flag from click
until the enqueue POST resolves, so the periodic status poll — which fires during the seconds the
server spends importing torch BEFORE the job exists — can no longer see `running=false` and re-enable
a mid-submit button. Button + progress now read Starting… → Running… → Run/Idle from one source.
