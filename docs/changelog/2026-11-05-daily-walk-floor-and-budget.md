# Walk floor resolved an hour early; spent budget marks uncounted

<!-- entry 563 -->

The walk's floor is resolved an hour before the window and visits starting before it are
dropped, or one under way at midnight is clipped by `since_id` onto the first day. A spent
page budget (`_MAX_DAILY_PAGES` = 40) sets `truncated` and makes the stop day and everything
older `uncounted`; the page banners only on an uncounted day in its own window.
