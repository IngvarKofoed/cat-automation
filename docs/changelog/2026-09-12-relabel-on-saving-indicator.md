# Re-label and send-back use the saving indicator

<!-- entry 297 -->

Labelled review's re-label and send-back are on the same indicator. A re-label deletes
the visit's crop files and cuts them all again, making it the SLOWEST write on the
page — and being awaited rather than optimistic, it previously left the page looking
completely idle for seconds. Root cause of the wait, unchanged: `_commit_label`
decodes + crops + writes one JPEG per visit frame, synchronously in the request.
