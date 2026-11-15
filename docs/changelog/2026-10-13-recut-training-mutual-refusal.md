# Re-cut and the training queue refuse each other

<!-- entry 452 -->

Re-cut and the training queue now refuse each other (409, both directions). A build
embedding the labelled set while a re-cut moves it reads a set that no longer exists as
read, and `_embed_items` skips a missing file in SILENCE — a quietly smaller gallery, not
an error. Advisory only: the two managers share no lock and the CLI bypasses it, which is
acceptable now that superseded crops are kept.
