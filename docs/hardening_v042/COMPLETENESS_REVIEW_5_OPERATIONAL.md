# Completeness review 5 — operational state persistence

## Findings

1. Boolean and non-integer state-size limits could pass constructor validation.
2. Disk state was size-checked before an unbounded read, allowing growth between check and read.
3. Resolving the final state path hid same-directory symlink aliases.

## Corrections

- state-size limits require a real positive integer or `None`;
- `lstat` plus opened-file inode comparison closes path-swap races;
- symlinks and non-regular files fail closed;
- reads are capped at `max_state_bytes + 1` after open;
- regression tests cover symlinks and post-`fstat` file growth.

## Result

State reads are bounded and resistant to symlink, path-swap and size-growth attacks.
