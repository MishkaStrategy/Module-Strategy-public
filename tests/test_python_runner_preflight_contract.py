from __future__ import annotations

from pathlib import Path


def _action_text() -> str:
    action = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "actions"
        / "python-runner-preflight"
        / "action.yml"
    )
    return action.read_text(encoding="utf-8")


def test_preflight_caches_dependencies_but_not_persistent_python_installations() -> None:
    text = _action_text()

    assert "enable-cache: true" in text
    assert "cache-python: false" in text
    assert "cache-dependency-glob: ${{ inputs.cache-dependency-glob }}" in text
    assert "cache-suffix: ${{ inputs.cache-suffix }}-py${{ inputs.python-version }}" in text
    assert "cache-local-path: ${{ runner.temp }}/setup-uv-cache-" in text


def test_preflight_preserves_exact_python_and_fail_closed_provisioning() -> None:
    text = _action_text()

    assert 'version: "0.11.31"' in text
    assert 'case "$requested" in' in text
    assert "3.12|3.13" in text
    assert 'uv python install "$requested"' in text
    assert 'if uv venv --python "$requested" --seed "$venv"; then' in text
    assert "Python environment seeding failed after" in text
    assert "Provisioned Python mismatch" in text


def test_preflight_documents_why_managed_python_cache_is_disabled() -> None:
    text = _action_text()

    assert "managed Python cache: disabled" in text
    assert "persistent self-hosted runners already provision Python" in text
