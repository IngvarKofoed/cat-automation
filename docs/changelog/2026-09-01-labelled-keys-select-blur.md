# Labelled mode's dropdown blurs so keys keep working

<!-- entry 242 -->

The Labelled keys were reachable only until you touched the mode's own "Show label" dropdown:
`onKey` drops any keystroke targeting a SELECT, so focus left there killed n/p AND every
re-label key — the mode read as having no navigation at all. It now blurs on change (entry
235's fix, same cause), and Prev/Next are visible buttons rather than hint-line-only keys.
