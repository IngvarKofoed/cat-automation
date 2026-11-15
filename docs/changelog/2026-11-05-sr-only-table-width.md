# sr-only on a table still widens the page

<!-- entry 566 -->

A `.sr-only` class on a `<table>` does not hide it from layout: a table never shrinks below its
content width, so `width: 1px` still widened the page into a sideways scroll (552 px on a
390 px phone at 90 days). The class goes on a wrapping div.
