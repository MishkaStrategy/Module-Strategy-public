# Round 03 — strict UTC boundary types

- Goal: make invalid timestamp types fail deterministically at the shared UTC boundary.
- Baseline: non-datetime values raised incidental `AttributeError` while accessing `.tzinfo`.
- Change: `utc_datetime` now rejects non-datetime values with a stable `TypeError` before timezone validation.
- Tests: non-datetime matrix plus aware offset normalization.
- No-look-ahead: timestamp normalization remains UTC-only and does not alter visibility or ordering.
- Compatibility: valid aware `datetime` objects behave identically.
