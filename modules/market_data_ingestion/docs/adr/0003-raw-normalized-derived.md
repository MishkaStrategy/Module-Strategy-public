# ADR-0003: Immutable raw, normalized and derived layers

**Status:** Accepted for local implementation checkpoint

- **Raw:** exact source bytes, immutable path, SHA-256, source metadata and a companion
  descriptor. Multi-page REST responses use a documented length-prefixed framing that
  preserves every original response body byte-for-byte.
- **Normalized:** strict UTC schema, deterministic order, exact duplicate removal with
  evidence, no price interpolation.
- **Derived:** only complete UTC-aligned groups; lineage points to normalized partitions.

A change to confirmed content creates a new dataset version. Healthy partitions are not
silently overwritten.
