# Error Discovery Audit Addendum

## PS-ERR-020 — optimization-sensitive runtime assertions

- **Severity:** High
- **Detected by:** independent final audit after publication of the initial 0.2.2 tree
- **Symptom:** Bandit B101 reported four `assert` statements in production source (`engine.py` and `module.py`).
- **Root cause:** internal invariants were expressed as runtime assertions. Python removes them under `-O`, so malformed internal state could bypass the intended fail-closed guard.
- **Reproduction:** `bandit -q -r src` and AST inspection of production source.
- **Fix:** replaced trigger assertions with explicit `None` checks returning false; replaced position assertions with explicit no-open-position handling / typed error.
- **Regression:** `tests/test_no_runtime_asserts.py` rejects any future `ast.Assert` in production source.
- **Repeated validation:** 229 tests passed; branch coverage 90%; Ruff/format/mypy/compileall passed; Bandit returned zero findings; wheel/sdist and clean-wheel smoke passed.
- **Status:** Fixed and published on the Draft PR branch.

This addendum is part of the mandatory Error Discovery evidence and must be read together with `ERROR_DISCOVERY_AUDIT.md`.
