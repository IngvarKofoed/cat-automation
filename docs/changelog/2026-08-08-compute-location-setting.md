# Compute stores a location setting gating day/night split

<!-- entry 122 -->

Compute-side location (lat/lon) setting gating the day/night split (admin-next P1): `Store.get_location`
/`set_location` persist to the settings KV; `GET`/`POST /api/location`. Seeds ONCE from the edge's
night-light config when unset — any failure / unreachable / out-of-range leaves it unset, never a
silent (0,0). POST validates range and rejects booleans (pydantic's bool-is-int coercion) and
NaN/Infinity; a new app-wide 422 handler sanitizes non-finite floats that would otherwise 500 the
response encoder. Non-finite stored values also degrade to "unset".
