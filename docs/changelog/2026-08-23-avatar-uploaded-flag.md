# cats overview gained avatar_uploaded distinct from has_avatar

<!-- entry 194 -->

`/api/cats/overview` gained `avatar_uploaded`, distinct from `has_avatar` (true for the
AUTO labelled-crop fallback too). Only an uploaded override can be deleted, so a remove
control needs to know which of the two it is showing — otherwise it offers to remove a
crop-derived photo and silently does nothing.
