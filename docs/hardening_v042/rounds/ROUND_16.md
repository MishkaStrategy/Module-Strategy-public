# Round 16 — atomic module decision preflight

Module decisions are now validated against isolated copies of feature and setup state before any real publish, delete, setup transition or broker command is applied. A late strict contract error cannot leave earlier parts of the same decision committed.
