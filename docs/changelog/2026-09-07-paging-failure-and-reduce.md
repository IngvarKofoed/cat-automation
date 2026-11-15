# Failed page says so; keyset bound uses reduce

<!-- entry 269 -->

Two smaller paging repairs: a FAILED page fetch triggered from the player now says
"older visits failed" in the readout (its error note lives on the page BEHIND the modal,
so the reader saw only a promise that never resolved); and the keyset bound uses `reduce`
rather than `Math.min(...spread)`, since paging made those arrays unbounded and a large
enough spread throws RangeError before the try block, as an invisible unhandled rejection.
