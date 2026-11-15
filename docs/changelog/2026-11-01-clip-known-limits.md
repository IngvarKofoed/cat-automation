# Clip limits: 45 s budget unmeasured, hard-kill temp dir

<!-- entry 546 -->

Known limits. The 45 s budget is unmeasured on the compute PC: on the loaded dev box 3000
frames took 17-44 s at 1280x720 and 5-19 s at 640x480, the slow end under `nice` — so the cap
may need lowering there. A HARD kill of the server mid-encode leaves that clip's temp dir in
the OS temp dir (graceful shutdown cleans up). Download from the home-screen app is unverified.
