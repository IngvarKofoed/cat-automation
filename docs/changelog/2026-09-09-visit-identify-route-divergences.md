# Visit-identify route works with no model and requires bounds

<!-- entry 282 -->

That route diverges from `/api/identify/run` twice, both because DETECT is the half
that resolves `unanalyzed` and is useful with no gallery: no active model is a SUCCESS
(detect ran, `identified: false`), and the deps check follows the HALVES — the
analyzer's always, the embedder's only when a model exists. Both span bounds are
REQUIRED and width-capped (10k ids): elsewhere a missing bound means "whole store",
and the household's phone calls this route on a no-auth LAN.
