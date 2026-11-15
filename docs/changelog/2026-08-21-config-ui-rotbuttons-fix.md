# Config UI rotButtons bug fixed

<!-- entry 186 -->

Fixed a pre-existing config-UI bug the AWB panel would have doubled: `rotButtons`
selected `.rot-btn`, which is also the shared STYLING for the focus (and now white
balance) "Auto" buttons. Clicking Auto therefore fired the rotation handler with
`Number(undefined)` = NaN → serialized `null` → a spurious 400 beside the real
request, and the active-state sync cleared the button's own highlight. Now scoped
to `.rot-btn[data-deg]`.
