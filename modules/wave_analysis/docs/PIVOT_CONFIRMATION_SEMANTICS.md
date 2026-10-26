# Pivot confirmation semantics

For a fractal with `right_bars=R`, an extremum at bar N is confirmed no earlier than the close of bar N+R. `pivot_time` records the extremum; `confirmed_at` records first availability.

Percentage, ATR and hybrid ZigZag pivots remain provisional until a completed opposite move reaches the active reversal threshold. ATR uses completed bars only.

Equal-level policy is explicit and deterministic: `earliest`, `latest` or `reject`.

Confirmed history is append-only and non-repainting. A data gap clears provisional detector context, starts a new segment and prevents a synthetic wave across the gap.
