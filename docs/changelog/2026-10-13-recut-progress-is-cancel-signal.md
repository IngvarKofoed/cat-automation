# recut on_progress doubles as the cancel signal

<!-- entry 450 -->

`recut`'s `on_progress` is now the cancel signal too (falsy stops), the convention
`embed_paths` defines and the managers produce — but it BREAKS and returns the partial
summary rather than raising as that one does, since a canceled run still has to report
what it moved. Load-bearing consequence: the CLI's own `progress` returned `None`, which
is falsy, so it had to start returning True or the CLI would stop after one batch and
print it as a clean success.
