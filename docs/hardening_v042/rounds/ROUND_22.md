# Round 22 — dependency-aware module quarantine

In non-strict mode, a module that fails during bar or event callbacks is quarantined after its first failure. Its published features are invalidated, feature-dependent modules are quarantined recursively, independent modules continue, and every successfully started module still receives `on_finish`.
