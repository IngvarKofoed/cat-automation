# Note warn/bad styles apply to the container's own class only

<!-- entry 471 -->

`.note.warn` / `.note.bad` are SAME-ELEMENT rules, so a nested `<span class="warn">`
inside a `.note` container styles nothing, and bare `.dim` has no rule at all. Every
new readout puts the state on the container's own class list. Entry 287's trap in CSS
form; caught only by reading COMPUTED STYLE in the browser (amber `rgb(216,166,87)`,
red `rgb(217,100,122)`), which is the check that rule demands.
