"""Every module must parse on the OLDEST Python the project supports, not just this box.

`compute.ps1` accepts 3.10+ and the compute PC runs 3.11, while this dev box runs 3.13.
Two f-string constructs PEP 701 legalised in 3.12 are SyntaxErrors before it:

1. a backslash anywhere inside a replacement field
2. reuse of the enclosing f-string's own delimiter inside a replacement field

Both fail at IMPORT, for the whole module — so one of them in `probe.py` stops
`compute.api.app` from starting at all, and the traceback points at a bare `)` rather
than at anything resembling the mistake. Entry 325 recorded the first occurrence; a
second one shipped anyway (entry 442), because `python -m compileall` on 3.13 accepts
both and the whole suite passes.

`ast.parse` cannot catch this — on 3.13 the constructs are valid, so the check has to
inspect the replacement fields itself. That makes it version-independent: it reports the
same violations whatever interpreter runs it, which is the only way a 3.13 box can guard
a 3.11 floor.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

# The delimiters an f-string can be written with, longest first so a triple-quoted string
# is never mistaken for a single-quoted one. That distinction is load-bearing: inside
# ``f"""..."""`` the delimiter is ``\"\"\"``, so a lone ``"`` in an expression was legal
# even pre-3.12 (it was the standard workaround) — collapsing it to one character reports
# three false positives in probe.py alone.
_DELIMS = ('"""', "'''", '"', "'")

_ROOTS = ("compute", "shared", "edge")


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _py_files() -> "list[pathlib.Path]":
    root = _repo_root()
    out: "list[pathlib.Path]" = []
    for name in _ROOTS:
        for f in sorted((root / name).rglob("*.py")):
            if "__pycache__" not in f.parts:
                out.append(f)
    return out


def _violations(path: pathlib.Path) -> "list[str]":
    """Pre-3.12-illegal replacement fields in one file, as human-readable lines."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    bad: "list[str]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        outer = ast.get_source_segment(src, node) or ""
        delim = next((d for d in _DELIMS if d in outer), None)
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            # The format spec is part of the replacement field, so it carries the same
            # restriction as the expression — and is easy to overlook.
            for sub in (part.value, part.format_spec):
                if sub is None:
                    continue
                seg = ast.get_source_segment(src, sub) or ""
                if "\\" in seg:
                    bad.append(f"{path.name}:{node.lineno}: backslash in f-string expr: {seg!r}")
                if delim and delim in seg:
                    bad.append(
                        f"{path.name}:{node.lineno}: delimiter {delim!r} reused in "
                        f"f-string expr: {seg!r}"
                    )
    return bad


def test_no_fstring_construct_that_needs_python_312():
    """The guard itself: scan every module, report every violation at once.

    Reports the whole list rather than failing on the first, since a sweep that renders
    HTML in f-strings tends to introduce several in one sitting.
    """
    files = _py_files()
    assert len(files) > 50, f"expected the repo's modules, found {len(files)} — bad root?"
    problems = [p for f in files for p in _violations(f)]
    assert not problems, (
        "these are SyntaxErrors on Python 3.11 (the compute PC) though they parse here:\n  "
        + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    "snippet, why",
    [
        # The backslash must sit inside a NESTED string literal, which is the only shape
        # that parses here and fails there. A bare `\\"` in the expression is a SyntaxError
        # on 3.13 too, so a snippet written that way would break this test file rather than
        # exercise the guard — the first draft of this test did exactly that.
        ('''x = f"{f' <span class=\\"sub\\">+{n}</span>' if n else ''}"''',
         "backslash inside a nested literal — entry 442's exact shape"),
        ('''x = f"{d["k"]}"''', "the f-string's own delimiter reused inside it"),
        ('''x = f"{v:{'\\t'}}"''', "backslash inside the format spec"),
    ],
)
def test_the_guard_actually_catches_each_construct(tmp_path, snippet, why):
    """Verified to FAIL against the real regressions, not merely to pass today.

    Without this, a checker that silently matched nothing would read as a clean repo
    forever — entries 411/433's hollow-test trap, in the guard rather than the feature.
    """
    f = tmp_path / "sample.py"
    f.write_text(snippet, encoding="utf-8")
    assert _violations(f), f"guard missed {why}: {snippet}"


def test_a_triple_quoted_fstring_may_hold_a_single_quote_char(tmp_path):
    """The false positive the delimiter rule exists to avoid.

    ``f\"\"\"{d[\"k\"]}\"\"\"`` is legal pre-3.12 — the delimiter is the triple quote — and
    probe.py's report builders rely on it. A guard that flagged this would be reverted
    the first time someone ran it, taking the real check with it.
    """
    f = tmp_path / "ok.py"
    f.write_text('d = {"k": 1}\nx = f"""{d["k"]}"""\n', encoding="utf-8")
    assert _violations(f) == []
