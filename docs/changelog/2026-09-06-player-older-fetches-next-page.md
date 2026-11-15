# Player Older fetches the next page at the last visit

<!-- entry 264 -->

The player's Older at the last loaded visit now fetches the next page instead of
refusing outright, so deep swiping still reaches back. It refuses THIS hop rather than
awaiting the page: the ease-back is the "that didn't take" cue, and holding the frame
still mid-gesture would read as a freeze. The note clears itself when the page lands,
since the hop it explained now works.
