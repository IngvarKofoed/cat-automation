# test_python_floor_syntax guards the Python floor

<!-- entry 443 -->

New `test_python_floor_syntax.py` guards the PYTHON FLOOR, not just this interpreter:
it walks every module's f-string replacement fields and rejects the two constructs PEP
701 legalised (a backslash, or reuse of the enclosing delimiter). `ast.parse` cannot
catch these — on 3.13 they are valid — so the check inspects the fields itself and
reports the same violations on any interpreter. Verified by re-injecting entry 442.
