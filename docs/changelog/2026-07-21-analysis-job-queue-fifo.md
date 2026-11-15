# Analysis jobs drain serially through a cancelable FIFO queue

<!-- entry 30 -->

Analysis job queue (FIFO, in-memory): enqueue replaces refuse-second-job 409; jobs drain serially with history.
Cancel current / Clear pending / Stop all controls + per-job terminal state (done/failed/canceled).
Addresses walk-away workflow — several buckets × oracles queued unattended, outcomes visible on return.
