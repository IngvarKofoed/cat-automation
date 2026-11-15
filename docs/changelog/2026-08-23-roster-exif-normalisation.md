# Roster duplicates client-side EXIF normalisation

<!-- entry 195 -->

The roster duplicates the user dashboard's client-side EXIF normalisation before
uploading an avatar. Not incidental: the server's cv2 re-encode DROPS the EXIF tag, so a
phone photo POSTed raw lands permanently sideways. Duplicated per the no-shared-JS
convention — the two front doors must both carry it or they disagree on the same upload.
