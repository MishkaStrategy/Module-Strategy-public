# Round 28 — bounded no-follow module configuration loading

YAML module configs are read through a bounded regular-file descriptor with symlink rejection, opened-file identity checks and strict UTF-8 decoding. YAML composition has a node cap, mapping keys are strings, and class/plugin fields no longer use implicit conversion.
