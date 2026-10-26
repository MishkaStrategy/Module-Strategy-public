# Round 26 — unambiguous canonical JSON values

Non-finite floats fail at the serialization boundary, negative zero is canonicalized, set members are ordered by canonical JSON, Enum values serialize recursively, and unsupported values raise explicit typed errors.
