# Round 25 — canonical string-keyed identity mappings

Serialization and run hashing no longer convert mapping keys through `str()`. Only exact string keys are accepted, preventing integer/string key collisions in JSON and run identity.
