# Admin loaded-count renders before grid early returns

<!-- entry 263 -->

Admin's loaded-count + Show-older state renders BEFORE `renderGrid`'s early returns.
It describes the LOADED SET, not what the filter left visible — and a grid reading
"all hidden by the filter" is exactly when an operator wants to pull older visits in,
so freezing the button there stranded them.
