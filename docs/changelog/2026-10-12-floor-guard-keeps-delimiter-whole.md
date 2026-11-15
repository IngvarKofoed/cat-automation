# Floor guard keeps triple-quote delimiters whole

<!-- entry 444 -->

That guard keeps the delimiter WHOLE (`\"\"\"` ≠ `\"`). Inside a triple-quoted f-string a
lone `"` was always legal and probe.py's report builders rely on it, so collapsing the
delimiter to one character reported three false positives there — a guard that cried
wolf would be deleted, taking the real check with it.
