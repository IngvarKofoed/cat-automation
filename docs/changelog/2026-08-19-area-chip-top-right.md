# Blob-area chip moved top-right with one shared renderer

<!-- entry 175 -->

The area chip moved to a tile's TOP-RIGHT and is one shared renderer across both grids. It sits
INSIDE the wrapping `.tchips` row via `margin-left:auto`, NOT pinned to the corner — pinning
chips to opposite corners is what silently overlapped on a narrow tile or a long cat name before
entry 153. Frame review renders it WITHOUT a bucket dot: that page holds no min/max thresholds,
so it states the number and makes no judgement; only the missed-visit panel colours it.
