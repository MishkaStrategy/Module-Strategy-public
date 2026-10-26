from __future__ import annotations

from pathlib import Path

from scripts.audit_setup_uv_manifest import ROOT, audit, setup_uv_uses

SETUP_UV_SHA = "37802adc94f370d6bfd71619e3f0bf239e1f3b78"  # pragma: allowlist secret
MANIFEST = "data:application/x-ndjson,%7B%22version%22%3A%220.11.31%22%7D"


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_valid_direct_setup_uv_step_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/valid.yml",
        f"""jobs:
  test:
    steps:
      - uses: astral-sh/setup-uv@{SETUP_UV_SHA}
        with:
          version: \"0.11.31\"
          manifest-file: \"{MANIFEST}\"
""",
    )

    assert audit(tmp_path) == []
    assert len(setup_uv_uses(tmp_path)) == 1


def test_composite_action_setup_uv_step_is_scanned(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/actions/python/action.yml",
        f"""runs:
  using: composite
  steps:
    - name: Install uv
      uses: astral-sh/setup-uv@{SETUP_UV_SHA}
      with:
        version: '0.11.31'
        manifest-file: '{MANIFEST}'
""",
    )

    assert audit(tmp_path) == []
    assert len(setup_uv_uses(tmp_path)) == 1


def test_missing_version_and_manifest_are_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/missing.yml",
        f"""jobs:
  test:
    steps:
      - uses: astral-sh/setup-uv@{SETUP_UV_SHA}
        with:
          enable-cache: false
""",
    )

    errors = audit(tmp_path)
    assert any("literal semantic version" in error for error in errors)
    assert any("data:application/x-ndjson" in error for error in errors)


def test_mutable_action_ref_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/mutable.yml",
        f"""jobs:
  test:
    steps:
      - uses: astral-sh/setup-uv@v7
        with:
          version: \"0.11.31\"
          manifest-file: \"{MANIFEST}\"
""",
    )

    assert any("immutable 40-character SHA" in error for error in audit(tmp_path))


def test_local_preflight_action_is_not_treated_as_direct_setup_uv(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/local.yml",
        """jobs:
  test:
    steps:
      - uses: ./.github/actions/python-runner-preflight
        with:
          role: fast
          python-version: \"3.12\"
""",
    )

    assert audit(tmp_path) == []
    assert setup_uv_uses(tmp_path) == []


def test_repository_direct_setup_uv_steps_are_hardened() -> None:
    assert audit(ROOT) == []
