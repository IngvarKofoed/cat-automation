# Edge reports its version, baked from git describe at launch

<!-- entry 11 -->

Edge reports its version. `edge.sh` resolves `git describe` once at launch and bakes it into
`CAT_EDGE_VERSION`; the server reads that env var (never shells out to git) and returns it on
`GET /status` as `version`, falling back to "unknown" when the bake step didn't run. Versioning
is git-tag-based — a new release is a new annotated tag, no code bump; first tag `v0.1.0`.
