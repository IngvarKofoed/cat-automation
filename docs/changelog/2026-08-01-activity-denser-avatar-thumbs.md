# Activity cards are denser with round avatar thumbnails

<!-- entry 84 -->

Activity feed made denser: dropped the subtitle and the "showing N" note, and folded the filter
(relabelled "Hide our cats") onto the heading line — more visits fit on screen.
Event thumbnails are now round and show the identified cat's AVATAR (falling back to the door frame
if that cat has no photo); an unknown/unidentified visit still shows the frame, where seeing the cat
is the point. The feed fetches `/api/cats/overview` alongside events so a photoless cat shows its
frame rather than 404-ing on an avatar. Frontend-only.
