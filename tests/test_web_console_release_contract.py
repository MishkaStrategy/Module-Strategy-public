from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts import web_console_release_contract as console_contract


def _binding(value: Any) -> dict[str, Any]:
    return {"status": "bound", "value": value}


def _pending() -> dict[str, Any]:
    return {"status": "pending", "value": None}


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _manifest(source_commit: str, *, decision: str = "accepted") -> dict[str, Any]:
    return {
        "schema_version": 2,
        "candidate": {
            "candidate_id": "a" * 64,
            "source": {"commit": source_commit},
            "components": [{"id": "console", "kind": "web_console"}],
        },
        "support_profile": {"profile_id": "b" * 64},
        "release_decision": {"decision": decision},
    }


def _git_source(root: Path) -> tuple[str, list[dict[str, str]]]:
    package = {
        "name": "fixture-console",
        "version": "1.0.0",
        "engines": {"node": ">=22.12.0"},
        "scripts": {"build": "tsc --noEmit && vite build"},
    }
    lock = {
        "name": "fixture-console",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": "fixture-console",
                "version": "1.0.0",
                "dependencies": {},
                "devDependencies": {},
                "optionalDependencies": {},
                "peerDependencies": {},
            }
        },
    }
    files = {
        "package.json": json.dumps(package, sort_keys=True).encode(),
        "package-lock.json": json.dumps(lock, sort_keys=True).encode(),
        "vite.config.ts": b"export default {};\n",
        "tsconfig.json": b"{}\n",
        "index.html": b"<main>console</main>\n",
        "src/main.ts": b"export {};\n",
    }
    for name, content in files.items():
        _write(root / "web/market-data-console" / name, content)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "web/market-data-console"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    inputs = [
        {
            "path": f"web/market-data-console/{name}",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(files.items())
    ]
    return commit, inputs


def _accepted_contract(
    root: Path, bundle: Path, manifest: dict[str, Any], inputs: list[dict[str, str]]
) -> dict[str, Any]:
    bundle.mkdir(parents=True, exist_ok=True)
    by_path = {row["path"]: row for row in inputs}
    package_sha = by_path["web/market-data-console/package.json"]["sha256"]
    lock_sha = by_path["web/market-data-console/package-lock.json"]["sha256"]
    artifact_members = [
        {"path": "assets/app.js", "size": 4, "sha256": hashlib.sha256(b"app!").hexdigest()},
        {"path": "index.html", "size": 7, "sha256": hashlib.sha256(b"<main/>").hexdigest()},
    ]
    artifact_buffer = io.BytesIO()
    with zipfile.ZipFile(artifact_buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for member in artifact_members:
            info = zipfile.ZipInfo(member["path"], date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, b"app!" if member["path"].startswith("assets/") else b"<main/>")
    artifact_bytes = artifact_buffer.getvalue()
    _write(bundle / "console.zip", artifact_bytes)
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    reconstruction = {
        "outcome": "passed",
        "isolated": True,
        "source_commit": manifest["candidate"]["source"]["commit"],
        "node_version": "22.12.0",
        "npm_version": "10.9.0",
        "lock_sha256": lock_sha,
    }
    receipt_bytes = json.dumps(reconstruction, sort_keys=True).encode()
    _write(bundle / "reconstruction.json", receipt_bytes)

    def receipt(name: str) -> dict[str, str]:
        content = f"fixture:{name}".encode()
        path = f"{name}.json"
        _write(bundle / path, content)
        return {"path": path, "sha256": hashlib.sha256(content).hexdigest()}

    linux_receipt = receipt("linux-evidence")
    darwin_receipt = receipt("darwin-evidence")
    source_commit = manifest["candidate"]["source"]["commit"]
    profile_id = manifest["support_profile"]["profile_id"]
    candidate_id = manifest["candidate"]["candidate_id"]
    contract = {
        "schema_version": 1,
        "contract_sha256": "0" * 64,
        "disposition": "accepted",
        "manifest": {
            "schema_version": 2,
            "manifest_sha256": _binding(
                hashlib.sha256(
                    console_contract.platform_manifest.canonical_json_bytes(manifest)
                ).hexdigest()
            ),
            "candidate_id": _binding(candidate_id),
            "profile_id": _binding(profile_id),
            "source_commit": _binding(source_commit),
        },
        "toolchain": {
            "node_version": _binding("22.12.0"),
            "npm_version": _binding("10.9.0"),
            "provisioning_material": _binding(
                {
                    "material_id": "fixture-material",
                    "source_uri": "https://example.invalid/node@sha256:" + "c" * 64,
                    "sha256": "c" * 64,
                }
            ),
            "registry_policy": _binding(
                {
                    "policy_id": "fixture-registry",
                    "registries": ["https://registry.example.invalid/"],
                }
            ),
            "install_script_policy": _binding({"mode": "disabled", "allowlist": []}),
        },
        "npm_lock": {
            "relation": _binding(
                {
                    "resolver": "npm",
                    "lockfile_version": 3,
                    "npm_version": "10.9.0",
                    "package_json_sha256": package_sha,
                    "package_lock_sha256": lock_sha,
                    "package_manager": "absent",
                }
            )
        },
        "linux_build": {
            "target_os": "linux",
            "architecture": _binding("x86_64"),
            "image": _binding(
                {"uri": "oci://build.example.invalid/node@sha256:" + "d" * 64, "sha256": "d" * 64}
            ),
            "recipe": _binding(
                {
                    "working_directory": "web/market-data-console",
                    "install_command": ["npm", "ci"],
                    "build_command": ["npm", "run", "build"],
                }
            ),
            "source_inputs": _binding(inputs),
            "reconstruction_receipt": _binding(
                {"path": "reconstruction.json", "sha256": hashlib.sha256(receipt_bytes).hexdigest()}
            ),
            "artifact": _binding(
                {
                    "path": "console.zip",
                    "sha256": artifact_sha,
                    "format": "zip",
                    "member_manifest_sha256": hashlib.sha256(
                        console_contract.canonical_json_bytes(artifact_members)
                    ).hexdigest(),
                    "members": artifact_members,
                }
            ),
        },
        "linux_serving": {
            "mode": _binding("static_asset"),
            "runtime": _binding(
                {"name": "fixture-server", "version": "1.2.3", "image_sha256": "e" * 64}
            ),
            "configuration": _binding([by_path["web/market-data-console/vite.config.ts"]]),
        },
        "linux_deployment": {
            "context": _binding(
                {
                    "environment_id": "linux-vds-fixture",
                    "os": "linux",
                    "architecture": "x86_64",
                    "host_image_sha256": "f" * 64,
                    "serving_image_sha256": "e" * 64,
                }
            ),
            "evidence": _binding(
                {
                    "evidence_id": "linux-deploy-1",
                    "role": "linux_deployment",
                    "context_id": "linux-vds-fixture",
                    "artifact_sha256": artifact_sha,
                    "outcome": "passed",
                    "receipt": linux_receipt,
                }
            ),
        },
        "darwin_acceptance": {
            "target_os": "darwin",
            "context": _binding(
                {
                    "environment_id": "darwin-acceptance-fixture",
                    "os": "darwin",
                    "architecture": "arm64",
                    "image_sha256": "1" * 64,
                }
            ),
            "browser_contexts": _binding(
                [
                    {
                        "id": "webkit-fixture",
                        "environment_id": "darwin-acceptance-fixture",
                        "browser": "webkit",
                        "version": "18.0",
                    }
                ]
            ),
            "artifact_use": _binding({"mode": "same_artifact", "artifact_sha256": artifact_sha}),
            "evidence": _binding(
                {
                    "evidence_id": "darwin-acceptance-1",
                    "role": "darwin_acceptance",
                    "context_id": "darwin-acceptance-fixture",
                    "artifact_sha256": artifact_sha,
                    "outcome": "passed",
                    "receipt": darwin_receipt,
                }
            ),
        },
        "blockers": [],
    }
    return console_contract.seal_contract(contract)


@pytest.fixture
def contract_case(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path / "source"
    root.mkdir()
    bundle = tmp_path / "bundle"
    commit, inputs = _git_source(root)
    manifest = _manifest(commit)
    contract = _accepted_contract(root, bundle, manifest, inputs)
    return root, bundle, manifest, contract


def _reseal(contract: dict[str, Any]) -> None:
    console_contract.seal_contract(contract)


def _replace_artifact(
    contract: dict[str, Any], bundle: Path, entries: list[tuple[str, bytes]]
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    artifact_bytes = buffer.getvalue()
    (bundle / "console.zip").write_bytes(artifact_bytes)
    members = [
        {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in sorted(entries)
    ]
    artifact = contract["linux_build"]["artifact"]["value"]
    artifact["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
    artifact["members"] = members
    artifact["member_manifest_sha256"] = hashlib.sha256(
        console_contract.canonical_json_bytes(members)
    ).hexdigest()
    _reseal(contract)


def test_pending_release_sidecar_is_auditable_without_selecting_owner_values() -> None:
    fixture = json.loads(Path("docs/release/WEB_CONSOLE_RELEASE_CONTRACT_V1.json").read_text())
    candidate = _manifest("1" * 40, decision="pending")
    console_contract.validate_contract(fixture, candidate, ".", ".")
    assert fixture["disposition"] == "pending"
    assert fixture["blockers"]
    assert "toolchain.node_version" in fixture["blockers"]
    assert "linux_build.reconstruction_receipt" in fixture["blockers"]
    assert "darwin_acceptance.browser_contexts" in fixture["blockers"]


def test_acceptance_requires_linked_v2_identity_and_no_substitution(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, bundle, manifest, contract = contract_case
    contract["darwin_acceptance"]["artifact_use"]["value"]["artifact_sha256"] = "9" * 64
    _reseal(contract)
    with pytest.raises(console_contract.WebConsoleContractError, match="substituted an artifact"):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_acceptance_rejects_a_missing_darwin_browser_context(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, bundle, manifest, contract = contract_case
    contract["darwin_acceptance"].pop("browser_contexts")
    _reseal(contract)
    with pytest.raises(
        console_contract.WebConsoleContractError, match="browser_contexts.*required"
    ):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_acceptance_rejects_linked_manifest_identity_mismatch(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, bundle, manifest, contract = contract_case
    manifest["candidate"]["candidate_id"] = "c" * 64
    contract["manifest"]["manifest_sha256"]["value"] = hashlib.sha256(
        console_contract.platform_manifest.canonical_json_bytes(manifest)
    ).hexdigest()
    _reseal(contract)
    with pytest.raises(
        console_contract.WebConsoleContractError,
        match="does not match linked v2 manifest candidate_id",
    ):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_acceptance_rejects_artifact_tampering(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, bundle, manifest, contract = contract_case
    with (bundle / "console.zip").open("ab") as artifact:
        artifact.write(b"tampered")
    with pytest.raises(console_contract.WebConsoleContractError, match="artifact digest mismatch"):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_artifact_compressed_size_limit_precedes_full_file_read(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, manifest, contract = contract_case
    artifact_path = (bundle / "console.zip").resolve()
    monkeypatch.setattr(console_contract.platform_manifest, "MAX_WEB_ASSET_ARCHIVE_BYTES", 1)
    original_open = Path.open

    def forbid_oversized_artifact_read(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == artifact_path:
            pytest.fail("oversized artifact was read before its compressed-size check")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_oversized_artifact_read)
    with pytest.raises(console_contract.WebConsoleContractError, match="file-size limit"):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_artifact_entry_count_limit_precedes_zipfile_materialization(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, manifest, contract = contract_case
    _replace_artifact(contract, bundle, [("a.js", b"a"), ("b.js", b"b")])
    monkeypatch.setattr(console_contract.platform_manifest, "MAX_WEB_ASSET_ARCHIVE_ENTRIES", 1)

    def forbidden_zipfile(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("ZipFile constructed before enforcing the central-directory entry bound")

    monkeypatch.setattr(console_contract.zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(console_contract.WebConsoleContractError, match="member-count limit"):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_artifact_declared_expanded_size_limit_precedes_member_reads(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, manifest, contract = contract_case
    monkeypatch.setattr(console_contract.platform_manifest, "MAX_WEB_ASSET_EXPANDED_BYTES", 10)

    def forbidden_member_read(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("ZIP member was read before enforcing the declared expanded-size bound")

    monkeypatch.setattr(console_contract.zipfile.ZipFile, "open", forbidden_member_read)
    with pytest.raises(console_contract.WebConsoleContractError, match="expanded-size limit"):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_artifact_stream_overrun_is_rejected_as_contract_error(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, manifest, contract = contract_case
    original_open = console_contract.zipfile.ZipFile.open

    def overlong_member(
        archive: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        mode: str = "r",
        pwd: bytes | None = None,
        *,
        force_zip64: bool = False,
    ) -> Any:
        info = name if isinstance(name, zipfile.ZipInfo) else archive.getinfo(name)
        if info.filename == "assets/app.js":
            return io.BytesIO(b"x" * (info.file_size + 1))
        return original_open(archive, name, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(console_contract.zipfile.ZipFile, "open", overlong_member)
    with pytest.raises(
        console_contract.WebConsoleContractError, match="exceeded its declared size"
    ):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_bundle_receipt_size_limit_precedes_full_file_read(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, manifest, contract = contract_case
    receipt_path = (bundle / "reconstruction.json").resolve()
    monkeypatch.setattr(console_contract, "MAX_WEB_CONSOLE_RECEIPT_BYTES", 1)
    original_open = Path.open

    def forbid_oversized_receipt_read(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == receipt_path:
            pytest.fail("oversized receipt was read before its file-size check")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_oversized_receipt_read)
    with pytest.raises(console_contract.WebConsoleContractError, match="file-size limit"):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_bundle_file_growth_after_stat_is_still_bounded(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, manifest, contract = contract_case
    artifact_path = (bundle / "console.zip").resolve()
    byte_limit = artifact_path.stat().st_size
    monkeypatch.setattr(
        console_contract.platform_manifest, "MAX_WEB_ASSET_ARCHIVE_BYTES", byte_limit
    )
    original_open = Path.open

    class TrackingStream(io.BytesIO):
        total_read = 0

        def read(self, size: int = -1) -> bytes:
            chunk = super().read(size)
            self.total_read += len(chunk)
            return chunk

    streams: list[TrackingStream] = []

    def grow_after_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == artifact_path:
            stream = TrackingStream(b"x" * (byte_limit + 20))
            streams.append(stream)
            return stream
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_after_stat)
    with pytest.raises(console_contract.WebConsoleContractError, match="file-size limit"):
        console_contract.validate_contract(contract, manifest, root, bundle)

    assert len(streams) == 1
    assert streams[0].total_read == byte_limit + 1


def test_sidecar_cannot_make_pending_manifest_acceptable(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, bundle, manifest, contract = contract_case
    manifest["release_decision"]["decision"] = "pending"
    with pytest.raises(console_contract.WebConsoleContractError, match="cannot accept a pending"):
        console_contract.validate_contract(contract, manifest, root, bundle)


def test_combined_candidate_requires_sidecar_and_preserves_v2_blockers(
    contract_case: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, manifest, contract = contract_case
    with pytest.raises(console_contract.WebConsoleContractError, match="sidecar"):
        console_contract.validate_combined_candidate(manifest, None, root, bundle)

    def reject_v2(*_args: Any, **_kwargs: Any) -> None:
        raise console_contract.platform_manifest.ManifestError("unknown-blocking Console scope")

    monkeypatch.setattr(console_contract.platform_manifest, "validate_candidate", reject_v2)
    with pytest.raises(console_contract.platform_manifest.ManifestError, match="unknown-blocking"):
        console_contract.validate_combined_candidate(manifest, contract, root, bundle)
