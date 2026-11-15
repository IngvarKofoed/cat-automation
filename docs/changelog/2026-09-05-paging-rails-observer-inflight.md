# Paging rails: observer re-check and unconditional in-flight clear

<!-- entry 262 -->

Rails the paging needed, each a real failure found in a browser. `IntersectionObserver`
fires on a CROSSING, so a page that didn't push the sentinel off screen (every visit
filtered out, or a short page) stalled the chain — it now re-checks by geometry and
continues. The in-flight flag is cleared UNCONDITIONALLY: skipping it when a page-1
reload superseded the fetch left it stuck true and blocked paging for the session.
A superseded older page is DISCARDED (its keyset was computed against the old set).
