from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import textwrap

import pytest
import yaml

from scripts import release_evidence_handoff


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / ".github" / "actions" / "publish-release-evidence" / "action.yml"


def _publisher() -> str:
    return PUBLISHER.read_text(encoding="utf-8")


def _encoded_handoff(entries: list[tuple[str, bytes, bytes | None]]) -> tuple[str, str]:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        for name, member_type, payload in entries:
            info = tarfile.TarInfo(name)
            info.type = member_type
            if member_type in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            else:
                info.linkname = payload.decode("utf-8") if payload else "target"
                archive.addfile(info)
    compressed = gzip.compress(tar_buffer.getvalue(), mtime=0)
    return (
        base64.b64encode(compressed).decode("ascii"),
        hashlib.sha256(compressed).hexdigest(),
    )


def _encoded_pax_header_chain(count: int) -> tuple[str, str]:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        for index in range(count):
            header = tarfile.TarInfo(f"pax-header-{index}")
            header.type = tarfile.XHDTYPE
            header.size = 0
            archive.addfile(header)
        archive.addfile(tarfile.TarInfo("evidence/result.json"), io.BytesIO(b"ok"))
    compressed = gzip.compress(tar_buffer.getvalue(), mtime=0)
    return base64.b64encode(compressed).decode("ascii"), hashlib.sha256(compressed).hexdigest()


def _regular_entry(name: str, payload: bytes = b"ok") -> tuple[str, bytes, bytes | None]:
    return name, tarfile.REGTYPE, payload


def test_handoff_rejects_decompression_bomb_before_tar_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compressed = gzip.compress(
        b"\0" * (release_evidence_handoff.MAX_DECOMPRESSED_TAR_BYTES + 1), mtime=0
    )
    payload = base64.b64encode(compressed).decode("ascii")
    digest = hashlib.sha256(compressed).hexdigest()
    monkeypatch.setattr(
        release_evidence_handoff.tarfile,
        "open",
        lambda *args, **kwargs: pytest.fail("tar parser ran before decompression limit"),
    )

    with pytest.raises(ValueError, match="maximum decompressed tar bytes"):
        release_evidence_handoff.decode_archive(payload, digest)


def test_handoff_rejects_too_many_regular_files() -> None:
    entries = [
        _regular_entry(f"evidence/{index:03d}", b"")
        for index in range(release_evidence_handoff.MAX_REGULAR_FILES + 1)
    ]
    payload, digest = _encoded_handoff(entries)

    with pytest.raises(ValueError, match="maximum regular-file count"):
        release_evidence_handoff.decode_archive(payload, digest)


def test_handoff_rejects_a_file_over_the_per_file_expansion_limit() -> None:
    entry = _regular_entry(
        "evidence/result.json", b"x" * (release_evidence_handoff.MAX_FILE_BYTES + 1)
    )
    payload, digest = _encoded_handoff([entry])

    with pytest.raises(ValueError, match="file exceeds maximum size"):
        release_evidence_handoff.decode_archive(payload, digest)


def test_handoff_rejects_total_expanded_file_bytes_over_limit() -> None:
    entries = [
        _regular_entry(f"evidence/{index}.json", b"x" * release_evidence_handoff.MAX_FILE_BYTES)
        for index in range(2)
    ]
    entries.append(_regular_entry("evidence/extra.json", b"x"))
    payload, digest = _encoded_handoff(entries)

    with pytest.raises(ValueError, match="maximum expanded file bytes"):
        release_evidence_handoff.decode_archive(payload, digest)


@pytest.mark.parametrize(
    "name",
    (
        "../escape.json",
        "nested/../../escape.json",
        "/absolute.json",
        "./dot-prefix.json",
        "nested//duplicate-separator.json",
        "nested\\windows-separator.json",
        "C:/drive-qualified.json",
    ),
)
def test_handoff_rejects_noncanonical_or_unsafe_posix_member_names(name: str) -> None:
    payload, digest = _encoded_handoff([_regular_entry(name)])

    with pytest.raises(ValueError, match="unsafe evidence archive entry"):
        release_evidence_handoff.decode_archive(payload, digest)


def test_handoff_rejects_duplicate_regular_member_names() -> None:
    payload, digest = _encoded_handoff(
        [
            _regular_entry("evidence/result.json", b"one"),
            _regular_entry("evidence/result.json", b"two"),
        ]
    )

    with pytest.raises(ValueError, match="duplicate evidence handoff entry"):
        release_evidence_handoff.decode_archive(payload, digest)


@pytest.mark.parametrize(
    "member_type",
    (
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.DIRTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
    ),
)
def test_handoff_rejects_nonregular_tar_members(member_type: bytes) -> None:
    payload, digest = _encoded_handoff([("evidence/special", member_type, b"target")])

    with pytest.raises(ValueError, match="unsupported evidence handoff entry"):
        release_evidence_handoff.decode_archive(payload, digest)


def test_handoff_rejects_file_path_prefix_collisions() -> None:
    payload, digest = _encoded_handoff(
        [_regular_entry("evidence", b"file"), _regular_entry("evidence/result.json", b"child")]
    )

    with pytest.raises(ValueError, match="conflicting evidence handoff entry"):
        release_evidence_handoff.decode_archive(payload, digest)


def test_handoff_rejects_casefolded_duplicate_paths() -> None:
    payload, digest = _encoded_handoff(
        [_regular_entry("evidence/Result.json"), _regular_entry("evidence/result.json")]
    )

    with pytest.raises(ValueError, match="duplicate evidence handoff entry"):
        release_evidence_handoff.decode_archive(payload, digest)


def test_pack_rejects_casefolded_duplicates_and_file_directory_conflicts(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "Result.json").write_bytes(b"one")
    (second_root / "result.json").write_bytes(b"two")
    with pytest.raises(ValueError, match="duplicate evidence archive entry"):
        release_evidence_handoff.build_archive(
            [first_root / "Result.json", second_root / "result.json"]
        )

    file_root = tmp_path / "file-source"
    directory_root = tmp_path / "directory-source"
    file_root.mkdir()
    directory_root.mkdir()
    (file_root / "A").write_bytes(b"file")
    (directory_root / "a").mkdir()
    (directory_root / "a" / "child.txt").write_bytes(b"child")
    with pytest.raises(ValueError, match="conflicting evidence archive entry"):
        release_evidence_handoff.build_archive([file_root / "A", directory_root / "a"])


def test_pack_output_round_trips_through_the_strict_decoder(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    (evidence / "nested").mkdir(parents=True)
    (evidence / "alpha.json").write_bytes(b"alpha")
    (evidence / "nested" / "beta.json").write_bytes(b"beta")

    archive = release_evidence_handoff.build_archive([evidence])
    payload, digest = release_evidence_handoff.encode_archive(archive)
    assert release_evidence_handoff.decode_archive(payload, digest) == archive


def test_pack_rejects_nested_symlinks(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    (evidence / "link.json").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink evidence entry"):
        release_evidence_handoff.build_archive([evidence])


def test_pack_bounds_source_entry_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for index in range(4):
        (evidence / f"entry-{index}.json").write_bytes(b"x")
    monkeypatch.setattr(release_evidence_handoff, "MAX_SOURCE_ENTRIES", 3)

    with pytest.raises(ValueError, match="maximum enumerated entry count"):
        release_evidence_handoff.build_archive([evidence])


def test_pack_rejects_file_swapped_to_symlink_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    victim = evidence / "victim.json"
    victim.write_bytes(b"original")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"secret")
    original_open = os.open
    swapped = False

    def swap_then_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if path == "victim.json" and kwargs.get("dir_fd") is not None and not swapped:
            victim.unlink()
            victim.symlink_to(outside)
            swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(release_evidence_handoff.os, "open", swap_then_open)
    monkeypatch.setattr(
        release_evidence_handoff.os,
        "supports_dir_fd",
        os.supports_dir_fd | {swap_then_open},
    )

    with pytest.raises(ValueError, match="unable to safely open evidence file"):
        release_evidence_handoff.build_archive([evidence])
    assert swapped


def test_handoff_rejects_chained_pax_headers_before_tarfile_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, digest = _encoded_pax_header_chain(1500)
    monkeypatch.setattr(
        release_evidence_handoff.tarfile,
        "open",
        lambda *args, **kwargs: pytest.fail("tarfile parser ran before extension-header preflight"),
    )

    with pytest.raises(ValueError, match="unsupported evidence handoff tar extension header"):
        release_evidence_handoff.decode_archive(payload, digest)


def _run_ambiguous_upload_scenario(
    tmp_path: Path, scenario: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    action = yaml.safe_load(_publisher())
    run_script = action["runs"]["steps"][0]["run"]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_path = tmp_path / "curl-state.json"
    exact_sha = "c" * 40
    run_id = "45678"
    attempt = "2"
    scoped_name = (
        f"Module-Strategy-{exact_sha}-ambiguous-test-run-{run_id}-attempt-{attempt}.tar.gz"
    )
    initial_assets: list[dict[str, object]] = []
    if scenario == "scoped_conflict":
        initial_assets.append(
            {
                "id": 99,
                "name": scoped_name,
                "content": base64.b64encode(b"old scoped bytes").decode("ascii"),
            }
        )
    state_path.write_text(
        json.dumps({"assets": initial_assets, "next_asset_id": 100, "upload_events": []}),
        encoding="utf-8",
    )

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import base64
            import json
            import os
            from pathlib import Path
            import sys
            from urllib.parse import parse_qs, urlsplit

            args = sys.argv[1:]

            def option(name):
                return args[args.index(name) + 1]

            output = Path(option("--output"))
            headers = Path(option("--dump-header"))
            method = option("--request")
            url = args[-1]
            state_path = Path(os.environ["FAKE_CURL_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            scenario = os.environ["FAKE_SCENARIO"]
            exact_sha = os.environ["EXPECTED_SHA"]
            repository = "MishkaStrategy/Module-Strategy"
            release = {
                "id": 77,
                "tag_name": f"ci-evidence-{exact_sha}",
                "target_commitish": exact_sha,
                "name": f"CI evidence {exact_sha}",
                "body": f"Canonical immutable evidence for {repository}@{exact_sha}.",
                "draft": True,
            }

            status = 500
            body = {"message": "unexpected fake GitHub API request"}
            binary_body = None
            if method == "GET" and url.endswith("/releases?per_page=100"):
                body = [release]
                status = 200
            elif method == "GET" and url.endswith(f"/repos/{repository}"):
                body = {"default_branch": "main"}
                status = 200
            elif method == "GET" and "/releases/77/assets?per_page=100" in url:
                state["asset_list_gets"] = state.get("asset_list_gets", 0) + 1
                if scenario == "failed_lookup" and state["asset_list_gets"] >= 2:
                    body = {"message": "temporary asset-list failure"}
                    status = 503
                else:
                    body = [
                        {"id": asset["id"], "name": asset["name"]}
                        for asset in state["assets"]
                    ]
                    status = 200
            elif method == "GET" and "/releases/assets/" in url:
                asset_id = int(url.rsplit("/", 1)[-1])
                asset = next(item for item in state["assets"] if item["id"] == asset_id)
                binary_body = base64.b64decode(asset["content"])
                status = 200
            elif method == "POST" and "/releases/77/assets?name=" in url:
                upload_name = parse_qs(urlsplit(url).query)["name"][0]
                data_arg = option("--data-binary")
                payload = Path(data_arg[1:]).read_bytes()
                state["upload_events"].append(upload_name)
                if upload_name.endswith("ambiguous-test.tar.gz") and "-run-" not in upload_name and not state.get("base_post_failed"):
                    state["base_post_failed"] = True
                    conflict = payload if scenario == "identical_post_timeout" else b"conflicting preexisting bytes"
                    state["assets"].append({
                        "id": state["next_asset_id"],
                        "name": upload_name,
                        "content": base64.b64encode(conflict).decode("ascii"),
                    })
                    state["next_asset_id"] += 1
                    body = {"message": "Upload body timed out due to inactivity"}
                    status = 408
                else:
                    state["assets"].append({
                        "id": state["next_asset_id"],
                        "name": upload_name,
                        "content": base64.b64encode(payload).decode("ascii"),
                    })
                    state["next_asset_id"] += 1
                    body = {"id": state["next_asset_id"] - 1, "name": upload_name}
                    status = 201

            if binary_body is None:
                output.write_text(json.dumps(body), encoding="utf-8")
            else:
                output.write_bytes(binary_body)
            headers.write_text(
                f"HTTP/1.1 {status} fake\\nX-RateLimit-Remaining: 4998\\n\\n",
                encoding="utf-8",
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            print(status, end="")
            """
        ),
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "result.txt").write_text("PASS\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "DRAFT_RELEASE": "true",
            "EVIDENCE_COMMIT_SHA": exact_sha,
            "EXPECTED_SHA": exact_sha,
            "FAKE_CURL_STATE": str(state_path),
            "FAKE_SCENARIO": scenario,
            "GH_TOKEN": "test-token",
            "GITHUB_API_URL": "https://api.github.test",
            "GITHUB_REPOSITORY": "MishkaStrategy/Module-Strategy",
            "GITHUB_RUN_ATTEMPT": attempt,
            "GITHUB_RUN_ID": run_id,
            "GITHUB_SERVER_URL": "https://github.com",
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PURPOSE_NAME": "ambiguous-test.tar.gz",
            "SOURCE_PATHS": str(evidence),
        }
    )
    completed = subprocess.run(
        ["bash", "-c", run_script],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, json.loads(state_path.read_text(encoding="utf-8"))


def test_release_publisher_captures_bounded_http_diagnostics() -> None:
    publisher = _publisher()

    assert "--dump-header" in publisher
    assert "--write-out '%{http_code}'" in publisher
    assert "api_error_message()" in publisher
    assert "[non-JSON response body omitted]" in publisher
    assert "message.replace" in publisher
    assert "[:400]" in publisher
    assert "[:200]" in publisher
    assert "GitHub API request failed:" in publisher
    assert "GitHub API error:" in publisher
    assert "GitHub API Retry-After:" in publisher
    assert "GitHub API rate limit:" in publisher
    assert "x-accepted-github-permissions" in publisher
    assert "x-github-request-id" in publisher
    assert "x-ratelimit-resource" in publisher
    assert "x-ratelimit-used" in publisher
    assert "GitHub API diagnostics:" in publisher
    assert 'echo "${GH_TOKEN}"' not in publisher


def test_release_publisher_uses_portable_response_header_tempfile() -> None:
    publisher = _publisher()

    assert 'mktemp "${temporary_directory}/github-response.headers.XXXXXX"' in publisher
    assert "XXXXXX.headers" not in publisher
    assert "Unable to allocate GitHub API response header file" in publisher


def test_release_publisher_retries_only_classified_transient_failures() -> None:
    publisher = _publisher()

    assert "local max_attempts=3 attempt=1" in publisher
    assert '[[ "${status}" == "429" ]]' in publisher
    assert '[[ "${status}" == "403" ]]' in publisher
    assert '[[ "${remaining}" == "0" ]]' in publisher
    assert '[[ "${lower_message}" == *"secondary rate limit"* ]]' in publisher
    assert '[[ "${lower_message}" == *"rate limit"* ]]' in publisher
    assert '[[ "${status}" =~ ^5[0-9][0-9]$ ]]' in publisher
    assert 'failure_class="permanent"' in publisher
    assert '"${failure_class}" == "permanent"' in publisher
    assert "delay=$((60 * (2 ** (attempt - 1))))" in publisher
    assert "((delay > 120)) && delay=120" in publisher
    assert 'sleep "${delay}"' in publisher
    assert "--retry-all-errors" not in publisher


def test_release_publisher_preserves_exact_sha_provenance_and_immutability() -> None:
    publisher = _publisher()

    assert 'tag="ci-evidence-${EVIDENCE_COMMIT_SHA}"' in publisher
    assert 'expected_release_name="CI evidence ${EVIDENCE_COMMIT_SHA}"' in publisher
    assert (
        'expected_release_body="Canonical immutable evidence for '
        '${GITHUB_REPOSITORY}@${EVIDENCE_COMMIT_SHA}."' in publisher
    )
    assert "if draft != 'true':" in publisher
    assert "payload['target_commitish'] = commit_sha" in publisher
    assert '[[ "${release_tag}" == "${tag}" ]]' in publisher
    assert '[[ "${release_name}" == "${expected_release_name}" ]]' in publisher
    assert '[[ "${release_body}" == "${expected_release_body}" ]]' in publisher
    assert '[[ "${release_draft}" == "${DRAFT_RELEASE}" ]]' in publisher
    assert (
        '"${target_commitish}" != "${EVIDENCE_COMMIT_SHA}" && '
        '"${target_commitish}" != "${default_branch}"' in publisher
    )
    assert "Existing draft evidence release target mismatch" in publisher
    assert "Existing published evidence release is not bound" in publisher
    assert "Refusing to replace differing immutable asset" in publisher
    assert "Concurrent immutable asset differs" in publisher
    assert "run_scoped_asset_name()" in publisher
    assert 'digest_name="${effective_asset_name}.sha256"' in publisher
    assert 'upload_immutable "${prepared_asset}" "${effective_asset_name}"' in publisher
    assert 'upload_immutable "${digest_path}" "${digest_name}"' in publisher
    assert "--clobber" not in publisher
    assert "--request DELETE" not in publisher
    assert "--request PATCH" not in publisher


def test_release_publisher_keeps_race_safe_create_and_upload_fallbacks() -> None:
    publisher = _publisher()

    assert "reconcile_release_after_create_failure()" in publisher
    assert "local max_attempts=6 attempt=1 delay=2" in publisher
    assert 'create_error="$(api_error_message "${create_response}")"' in publisher
    assert 'reconciled_release="$(reconcile_release_after_create_failure)"' in publisher
    assert "Release creation failed after bounded reconciliation" in publisher
    assert "Evidence upload failed and no concurrent immutable asset appeared" in publisher
    assert 'if asset_id="$(find_asset_id "${upload_name}")"; then' in publisher
    assert 'download_asset "${asset_id}" "${existing_directory}/${upload_name}"' in publisher


def test_timed_out_upload_with_differing_asset_switches_once_and_binds_final_digest(
    tmp_path: Path,
) -> None:
    completed, state = _run_ambiguous_upload_scenario(tmp_path, "different_post_timeout")

    assert completed.returncode == 0, completed.stderr
    assert "Preserving the differing immutable asset and switching once" in completed.stdout
    assert "Verified immutable evidence archive and digest:" in completed.stdout
    exact_sha = "c" * 40
    base_name = f"Module-Strategy-{exact_sha}-ambiguous-test.tar.gz"
    scoped_name = f"Module-Strategy-{exact_sha}-ambiguous-test-run-45678-attempt-2.tar.gz"
    digest_name = f"{scoped_name}.sha256"
    events = state["upload_events"]
    assert events == [base_name, scoped_name, digest_name]
    assets = state["assets"]
    assert isinstance(assets, list)
    by_name = {asset["name"]: base64.b64decode(asset["content"]) for asset in assets}
    assert by_name[base_name] == b"conflicting preexisting bytes"
    scoped_archive = by_name[scoped_name]
    expected_digest = hashlib.sha256(scoped_archive).hexdigest()
    assert by_name[digest_name] == f"{expected_digest}  {scoped_name}\n".encode()


def test_timed_out_upload_fails_closed_on_run_scoped_collision(tmp_path: Path) -> None:
    completed, state = _run_ambiguous_upload_scenario(tmp_path, "scoped_conflict")

    assert completed.returncode != 0
    assert (
        "Run-scoped evidence archive upload failed; refusing another fallback" in completed.stderr
    )
    assert "Verified immutable evidence archive and digest:" not in completed.stdout
    assert not any("-run-45678-attempt-2" in name for name in state["upload_events"])
    assets = state["assets"]
    assert isinstance(assets, list)
    exact_sha = "c" * 40
    base_name = f"Module-Strategy-{exact_sha}-ambiguous-test.tar.gz"
    scoped_name = f"Module-Strategy-{exact_sha}-ambiguous-test-run-45678-attempt-2.tar.gz"
    by_name = {asset["name"]: base64.b64decode(asset["content"]) for asset in assets}
    assert by_name[base_name] == b"conflicting preexisting bytes"
    assert by_name[scoped_name] == b"old scoped bytes"
    assert not any(name.endswith(".sha256") for name in by_name)


def test_timed_out_upload_with_identical_readback_remains_idempotent(tmp_path: Path) -> None:
    completed, state = _run_ambiguous_upload_scenario(tmp_path, "identical_post_timeout")

    assert completed.returncode == 0, completed.stderr
    assert "switching once" not in completed.stdout
    assert "Verified immutable evidence archive and digest:" in completed.stdout
    exact_sha = "c" * 40
    base_name = f"Module-Strategy-{exact_sha}-ambiguous-test.tar.gz"
    assert state["upload_events"] == [base_name, f"{base_name}.sha256"]


def test_timed_out_upload_does_not_fallback_when_asset_lookup_fails(tmp_path: Path) -> None:
    completed, state = _run_ambiguous_upload_scenario(tmp_path, "failed_lookup")

    assert completed.returncode != 0
    assert "status=503" in completed.stderr
    assert (
        "Evidence archive upload failed; refusing a fallback without a verified conflicting asset"
        in completed.stderr
    )
    assert "switching once" not in completed.stdout
    assert "Verified immutable evidence archive and digest:" not in completed.stdout
    assert not any("-run-45678-attempt-2" in name for name in state["upload_events"])


def test_release_publisher_creates_draft_container_on_default_branch_without_target(
    tmp_path: Path,
) -> None:
    action = yaml.safe_load(PUBLISHER.read_text(encoding="utf-8"))
    run_script = action["runs"]["steps"][0]["run"]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_path = tmp_path / "curl-state.json"
    state_path.write_text("{}", encoding="utf-8")

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]

            def option(name: str) -> str:
                index = args.index(name)
                return args[index + 1]

            output = Path(option("--output"))
            headers = Path(option("--dump-header"))
            method = option("--request")
            url = args[-1]
            state_path = Path(os.environ["FAKE_CURL_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            exact_sha = os.environ["EXPECTED_SHA"]
            exact_tag = os.environ["EXPECTED_TAG"]
            repository = "MishkaStrategy/Module-Strategy"
            release = {
                "id": 88,
                "tag_name": exact_tag,
                "target_commitish": "main",
                "name": f"CI evidence {exact_sha}",
                "body": f"Canonical immutable evidence for {repository}@{exact_sha}.",
                "draft": True,
            }

            status = 500
            body = {"message": "unexpected fake GitHub API request"}
            if method == "GET" and url.endswith("/releases?per_page=100"):
                state["release_gets"] = state.get("release_gets", 0) + 1
                body = []
                status = 200
            elif method == "POST" and url.endswith("/releases"):
                state["create_posts"] = state.get("create_posts", 0) + 1
                payload = json.loads(option("--data-binary"))
                state["create_payload"] = payload
                body = release
                status = 201
            elif method == "GET" and url.endswith(f"/repos/{repository}"):
                state["repository_gets"] = state.get("repository_gets", 0) + 1
                body = {"default_branch": "main"}
                status = 200
            elif method == "GET" and "/releases/88/assets?per_page=100" in url:
                state["asset_gets"] = state.get("asset_gets", 0) + 1
                body = []
                status = 200
            elif method == "POST" and "/releases/88/assets?name=" in url:
                state["upload_posts"] = state.get("upload_posts", 0) + 1
                body = {"id": 200 + state["upload_posts"]}
                status = 201

            output.write_text(json.dumps(body), encoding="utf-8")
            headers.write_text(
                f"HTTP/1.1 {status} fake\\nX-RateLimit-Remaining: 4998\\n\\n",
                encoding="utf-8",
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            print(status, end="")
            """
        ),
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "result.txt").write_text("PASS\n", encoding="utf-8")

    exact_sha = "b" * 40
    env = os.environ.copy()
    env.update(
        {
            "DRAFT_RELEASE": "true",
            "EVIDENCE_COMMIT_SHA": exact_sha,
            "EXPECTED_SHA": exact_sha,
            "EXPECTED_TAG": f"ci-evidence-{exact_sha}",
            "FAKE_CURL_STATE": str(state_path),
            "GH_TOKEN": "test-token",
            "GITHUB_API_URL": "https://api.github.test",
            "GITHUB_REPOSITORY": "MishkaStrategy/Module-Strategy",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "23456",
            "GITHUB_SERVER_URL": "https://github.com",
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PURPOSE_NAME": "default-branch-test.tar.gz",
            "SOURCE_PATHS": str(evidence),
        }
    )

    completed = subprocess.run(
        ["bash", "-c", run_script],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["release_gets"] == 1
    assert state["create_posts"] == 1
    assert state["repository_gets"] == 1
    assert state["upload_posts"] == 2
    payload = state["create_payload"]
    assert payload["tag_name"] == f"ci-evidence-{exact_sha}"
    assert payload["name"] == f"CI evidence {exact_sha}"
    assert payload["body"] == (
        f"Canonical immutable evidence for MishkaStrategy/Module-Strategy@{exact_sha}."
    )
    assert payload["draft"] is True
    assert "target_commitish" not in payload


def test_release_publisher_recovers_when_concurrent_release_visibility_is_delayed(
    tmp_path: Path,
) -> None:
    action = yaml.safe_load(PUBLISHER.read_text(encoding="utf-8"))
    run_script = action["runs"]["steps"][0]["run"]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_path = tmp_path / "curl-state.json"
    state_path.write_text("{}", encoding="utf-8")

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]

            def option(name: str) -> str:
                index = args.index(name)
                return args[index + 1]

            output = Path(option("--output"))
            headers = Path(option("--dump-header"))
            method = option("--request")
            url = args[-1]
            state_path = Path(os.environ["FAKE_CURL_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            exact_sha = os.environ["EXPECTED_SHA"]
            repository = "MishkaStrategy/Module-Strategy"
            release = {
                "id": 77,
                "tag_name": os.environ["EXPECTED_TAG"],
                "target_commitish": exact_sha,
                "name": f"CI evidence {exact_sha}",
                "body": f"Canonical immutable evidence for {repository}@{exact_sha}.",
                "draft": True,
            }

            status = 500
            body = {"message": "unexpected fake GitHub API request"}
            if method == "GET" and url.endswith("/releases?per_page=100"):
                state["release_gets"] = state.get("release_gets", 0) + 1
                body = [] if state["release_gets"] <= 2 else [release]
                status = 200
            elif method == "POST" and url.endswith("/releases"):
                state["create_posts"] = state.get("create_posts", 0) + 1
                body = {
                    "message": "Resource not accessible by integration",
                    "documentation_url": "https://docs.github.test/create-a-release",
                }
                status = 403
            elif method == "GET" and url.endswith(f"/repos/{repository}"):
                state["repository_gets"] = state.get("repository_gets", 0) + 1
                body = {"default_branch": "main"}
                status = 200
            elif method == "GET" and "/releases/77/assets?per_page=100" in url:
                state["asset_gets"] = state.get("asset_gets", 0) + 1
                body = []
                status = 200
            elif method == "POST" and "/releases/77/assets?name=" in url:
                state["upload_posts"] = state.get("upload_posts", 0) + 1
                body = {"id": 100 + state["upload_posts"]}
                status = 201

            output.write_text(json.dumps(body), encoding="utf-8")
            headers.write_text(
                f"HTTP/1.1 {status} fake\\nX-RateLimit-Remaining: 4998\\n\\n",
                encoding="utf-8",
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            print(status, end="")
            """
        ),
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "result.txt").write_text("PASS\n", encoding="utf-8")

    exact_sha = "a" * 40
    env = os.environ.copy()
    env.update(
        {
            "DRAFT_RELEASE": "true",
            "EVIDENCE_COMMIT_SHA": exact_sha,
            "EXPECTED_SHA": exact_sha,
            "EXPECTED_TAG": f"ci-evidence-{exact_sha}",
            "FAKE_CURL_STATE": str(state_path),
            "GH_TOKEN": "test-token",
            "GITHUB_API_URL": "https://api.github.test",
            "GITHUB_REPOSITORY": "MishkaStrategy/Module-Strategy",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_SERVER_URL": "https://github.com",
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PURPOSE_NAME": "race-test.tar.gz",
            "SOURCE_PATHS": str(evidence),
        }
    )

    completed = subprocess.run(
        ["bash", "-c", run_script],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "GitHub API request failed: method=POST status=403" in completed.stderr
    assert "Concurrent exact-SHA evidence release not visible yet" in completed.stderr
    assert (
        "Reconciled concurrent exact-SHA evidence release after create failure" in completed.stderr
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["release_gets"] == 3
    assert state["create_posts"] == 1
    assert state["repository_gets"] == 1
    assert state["upload_posts"] == 2
