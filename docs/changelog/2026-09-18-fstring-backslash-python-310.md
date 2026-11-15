# Confusion-table f-strings no longer put a backslash inside braces

<!-- entry 325 -->

The confusion-table f-strings no longer put a BACKSLASH inside `{}`. That is a
SyntaxError before Python 3.12 (PEP 701 lifted it) and it fails at IMPORT for the
whole module, so `compute.api.app` would not have started at all — `compute.ps1`
accepts 3.10+. Invisible here: this dev box runs 3.13.
