# Skip checkbox blurs itself and re-renders nothing

<!-- entry 513 -->

It blurs itself on change — entries 235/242/298's trap, now reachable in this modal,
whose keydown handler drops any keystroke targeting an INPUT — and, unlike the two
header toggles, closes the player and re-renders NOTHING: it changes what the nav can
reach, not what the list contains.
