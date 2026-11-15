# Day-group cursor reset and empty-state clear on append

<!-- entry 267 -->

Paging rails the browser pass missed, all in the append path. The day-group cursor is
reset beside `railEl.innerHTML = ''`, not in the non-empty branch the empty path returns
before reaching — a cursor pointing at a detached rail made an appended page's rows
invisible while still counted in `activityVisible`, so the position readout and the
player's nav addressed rows not on screen. Appending also clears the empty-state panel,
which otherwise kept claiming "nothing to show" above the row just added.
