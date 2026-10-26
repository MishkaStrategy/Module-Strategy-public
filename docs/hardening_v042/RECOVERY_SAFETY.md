# Recovery safety rules

- The recovery branch must never be force-pushed.
- The stable `core/hardening-v0.4.x` branch remains untouched until the recovery head has exact-head green evidence.
- The temporary recovery Pull Request must not be merged.
- Promotion is a normal fast-forward of the stable hardening ref after review of the final diff and checks.
- Transport archives, base64 parts, materializers, debug logs, local environments, caches, datasets and secrets are forbidden in the promoted diff.
- No merge into the integration or main branch, tag, release or auto-merge is authorized.
