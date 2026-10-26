# Round 21 — deterministic module exception policy

Module start and bar failures now produce stable typed skip reasons in non-strict mode. Every successfully started module receives shutdown. A standalone `on_finish` failure is raised in strict mode, while shutdown failures never mask an earlier primary exception.
