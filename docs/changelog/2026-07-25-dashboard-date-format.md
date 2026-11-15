# Dashboard dates use dd/mm-yyyy

<!-- entry 51 -->

Dashboard date format fixed to `dd/mm-yyyy` (e.g. `12/07-2026`), superseding the locale's
m/d/y order. formatTime now builds the string by hand (local time) since no locale yields the
mixed "/" then "-" separators. Presentation only.
