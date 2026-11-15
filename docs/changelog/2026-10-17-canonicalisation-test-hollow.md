# Canonicalisation test now reads the stamp off the committed crop

<!-- entry 472 -->

A canonicalisation test that drove `POST /api/crop-geometry` asserted NOTHING: the
endpoint canonicalises before storing, so the read path could stamp `raw` verbatim and
the test still passed — proven by injecting exactly that. It now writes the
non-canonical value the way a hand-edit would and reads the stamp off the committed
CROP. Entries 411/433/466's hollow-test trap, fourth instance, same tell.
