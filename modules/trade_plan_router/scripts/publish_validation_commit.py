from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


CODE_PATHS = (
    "modules/trade_plan_router/src/modular_strategy_trade_plan_router/canonical.py",
    "modules/trade_plan_router/src/modular_strategy_trade_plan_router/decimal_math.py",
    "modules/trade_plan_router/src/modular_strategy_trade_plan_router/models.py",
    "modules/trade_plan_router/src/modular_strategy_trade_plan_router/module.py",
    "modules/trade_plan_router/src/modular_strategy_trade_plan_router/router.py",
    "modules/trade_plan_router/src/modular_strategy_trade_plan_router/serialization.py",
    "modules/trade_plan_router/scripts/apply_static_validation_fixes.py",
    "modules/trade_plan_router/scripts/apply_static_validation_fixes_v2.py",
)
REPORT_PATHS = (
    "reports/trade_plan_router/STATIC_FIX_DIAGNOSTIC.txt",
    "reports/trade_plan_router/STANDALONE_TEST_RECOVERY.txt",
    "reports/trade_plan_router/LARGE_TEST_ARCHIVE_RECOVERY.txt",
    "reports/trade_plan_router/VALIDATION_GATE.txt",
    "reports/trade_plan_router/CHECK_CODES.json",
    "reports/trade_plan_router/ruff.txt",
    "reports/trade_plan_router/mypy.txt",
    "reports/trade_plan_router/pytest.txt",
    "reports/trade_plan_router/coverage.txt",
)
TEST_ROOT = Path("modules/trade_plan_router/tests")


class PublisherError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, repository: str, token: str) -> None:
        self._base = f"https://api.github.com/repos/{repository}"
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "decision-router-validation-publisher",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _curl(
        method: str,
        url: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> tuple[int, bytes]:
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--request",
            method,
            "--write-out",
            "\n%{http_code}",
        ]
        for name, value in headers.items():
            command.extend(("--header", f"{name}: {value}"))
        if data is not None:
            command.extend(("--data-binary", "@-"))
        command.append(url)
        completed = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise PublisherError(f"curl transport failed: {stderr}")
        try:
            body, status_raw = completed.stdout.rsplit(b"\n", 1)
            status = int(status_raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PublisherError("curl response did not include an HTTP status") from exc
        return status, body

    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        data = None
        headers = dict(self._headers)
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = f"{self._base}{endpoint}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                status = response.status
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except URLError:
            status, raw = self._curl(method, url, headers, data)

        if allow_not_found and status == 404:
            return None
        if not 200 <= status < 300:
            body = raw.decode("utf-8", errors="replace")
            raise PublisherError(f"GitHub API {method} {endpoint} failed with {status}: {body}")
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublisherError(f"GitHub API returned invalid JSON for {endpoint}") from exc
        if not isinstance(value, dict):
            raise PublisherError(f"unexpected GitHub API response for {endpoint}")
        return value


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PublisherError(f"required environment variable is missing: {name}")
    return value


def _remote_content(
    client: GitHubClient,
    path: str,
    ref: str,
) -> bytes | None:
    encoded_path = quote(path, safe="/")
    encoded_ref = quote(ref, safe="")
    response = client.request(
        "GET",
        f"/contents/{encoded_path}?ref={encoded_ref}",
        allow_not_found=True,
    )
    if response is None:
        return None
    if response.get("type") != "file":
        raise PublisherError(f"candidate path is not a file: {path}")
    encoded = response.get("content")
    if not isinstance(encoded, str):
        raise PublisherError(f"GitHub did not return inline content for {path}")
    try:
        return base64.b64decode(encoded.replace("\n", ""), validate=True)
    except ValueError as exc:
        raise PublisherError(f"invalid base64 content returned for {path}") from exc


def _blob(client: GitHubClient, content: bytes) -> str:
    response = client.request(
        "POST",
        "/git/blobs",
        {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        },
    )
    assert response is not None
    sha = response.get("sha")
    if not isinstance(sha, str) or not sha:
        raise PublisherError("GitHub blob creation did not return a SHA")
    return sha


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def _test_paths() -> tuple[str, ...]:
    if not TEST_ROOT.is_dir():
        raise PublisherError("Router test root is missing")
    return tuple(
        sorted(
            path.as_posix()
            for path in TEST_ROOT.rglob("*")
            if path.is_file() and path.suffix in {".py", ".json"}
        )
    )


def _validated_paths() -> tuple[str, ...]:
    codes_path = Path("reports/trade_plan_router/CHECK_CODES.json")
    if not codes_path.is_file():
        raise PublisherError("validation code report is missing")
    try:
        codes = json.loads(codes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublisherError("validation code report is invalid") from exc
    if not isinstance(codes, dict):
        raise PublisherError("validation code report must be a JSON object")
    pytest_code = codes.get("pytest")
    if not isinstance(pytest_code, int) or isinstance(pytest_code, bool):
        raise PublisherError("validation report is missing pytest exit code")
    if pytest_code == 0:
        print("pytest passed: publishing verified code changes, tests and reports")
        return tuple(dict.fromkeys((*CODE_PATHS, *_test_paths(), *REPORT_PATHS)))
    print("pytest failed: publishing reports only; code and tests remain unchanged")
    return REPORT_PATHS


def publish() -> int:
    repository = _required_environment("GITHUB_REPOSITORY")
    token = _required_environment("GITHUB_TOKEN")
    branch = _required_environment("ROUTER_BRANCH")
    expected_head = _required_environment("PR_HEAD_SHA")
    client = GitHubClient(repository, token)

    branch_path = quote(branch, safe="/")
    ref_response = client.request("GET", f"/git/ref/heads/{branch_path}")
    assert ref_response is not None
    ref_object = ref_response.get("object")
    if not isinstance(ref_object, dict):
        raise PublisherError("branch ref response is missing object")
    actual_head = ref_object.get("sha")
    if actual_head != expected_head:
        raise PublisherError(
            f"Router branch moved during validation: expected {expected_head}, found {actual_head}"
        )

    commit_response = client.request("GET", f"/git/commits/{actual_head}")
    assert commit_response is not None
    tree_object = commit_response.get("tree")
    if not isinstance(tree_object, dict) or not isinstance(tree_object.get("sha"), str):
        raise PublisherError("head commit response is missing tree SHA")
    base_tree = tree_object["sha"]

    entries: list[dict[str, Any]] = []
    for path in _validated_paths():
        local_path = Path(path)
        local_content = local_path.read_bytes() if local_path.is_file() else None
        remote_content = _remote_content(client, path, actual_head)
        if local_content == remote_content:
            continue
        if local_content is None:
            if remote_content is None:
                continue
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            print(f"DELETE {path}")
            continue
        entries.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": _blob(client, local_content),
            }
        )
        print(f"UPDATE {path}")

    if not entries:
        print("No validation changes to publish")
        _write_output("commit_sha", actual_head)
        _write_output("published", "false")
        return 0

    tree_response = client.request(
        "POST",
        "/git/trees",
        {"base_tree": base_tree, "tree": entries},
    )
    assert tree_response is not None
    new_tree = tree_response.get("sha")
    if not isinstance(new_tree, str) or not new_tree:
        raise PublisherError("GitHub tree creation did not return a SHA")

    new_commit_response = client.request(
        "POST",
        "/git/commits",
        {
            "message": "Apply Router validation fixes and record gate [skip ci]",
            "tree": new_tree,
            "parents": [actual_head],
        },
    )
    assert new_commit_response is not None
    new_commit = new_commit_response.get("sha")
    if not isinstance(new_commit, str) or not new_commit:
        raise PublisherError("GitHub commit creation did not return a SHA")

    client.request(
        "PATCH",
        f"/git/refs/heads/{branch_path}",
        {"sha": new_commit, "force": False},
    )
    print(f"Published validation commit {new_commit}")
    _write_output("commit_sha", new_commit)
    _write_output("published", "true")
    return 0


def main() -> int:
    try:
        return publish()
    except PublisherError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
