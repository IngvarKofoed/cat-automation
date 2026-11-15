# Admin Tools page resolves geometry and housekeeping from the browser

<!-- entry 445 -->

New admin **Tools** page (last tab) resolves the geometry blocker from the browser: a
census of what crop shapes the labelled set holds, a target picker, a per-branch plan,
and a cancellable job. The four messages that sent the operator to `compute.tools.recut_crops`
now name the page instead. Housekeeping moved here too (non-motion purge, orphan sweep,
Clear all frames); Start narrows to stage + collection.
Spec: docs/specs/2026-08-10-admin-tools-page.md.
