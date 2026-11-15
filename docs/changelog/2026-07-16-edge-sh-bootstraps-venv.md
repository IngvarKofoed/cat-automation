# edge.sh bootstraps the venv and launches the edge server

<!-- entry 5 -->

`./edge.sh` is the entrypoint to run the edge server: it bootstraps `.venv` from
`edge/requirements.txt` on first run, then launches `edge.server.app` (honors
`CAT_EDGE_PORT`). One command to start the edge on a fresh checkout.
