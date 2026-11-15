# Dev proxy streams via pooled httpx and stops on Ctrl-C

<!-- entry 109 -->

Frontend dev proxy no longer wedges or ignores Ctrl-C: a BLOCKING `requests` call in an async handler,
buffering the dashboard's endless SSE stream (`/api/events/stream`), parked the event loop (stuck in a
C-level read, so the interrupt never fired); no pooling also churned a TCP handshake per `/media` image.
Now a pooled `httpx.AsyncClient` that STREAMS responses, plus uvicorn `timeout_graceful_shutdown=5` so
one Ctrl-C stops it mid-stream. `httpx` added to compute/requirements.txt (was only transitive).
