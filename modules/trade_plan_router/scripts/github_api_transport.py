from __future__ import annotations

import json
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GitHubTransportError(RuntimeError):
    pass


def _curl_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--request",
        "GET",
        "--write-out",
        "\n%{http_code}",
    ]
    for name, value in headers.items():
        command.extend(("--header", f"{name}: {value}"))
    command.append(url)
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise GitHubTransportError(f"curl transport failed: {stderr}")
    try:
        body, status_raw = completed.stdout.rsplit(b"\n", 1)
        status = int(status_raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitHubTransportError("curl response did not include an HTTP status") from exc
    if not 200 <= status < 300:
        detail = body.decode("utf-8", errors="replace")
        raise GitHubTransportError(f"GitHub API returned {status}: {detail}")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubTransportError("GitHub API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise GitHubTransportError("GitHub API JSON root must be an object")
    return value


def get_json(url: str, headers: dict[str, str], *, timeout: int = 60) -> dict[str, Any]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubTransportError(f"GitHub API returned {exc.code}: {detail}") from exc
    except URLError:
        return _curl_json(url, headers)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubTransportError("GitHub API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise GitHubTransportError("GitHub API JSON root must be an object")
    return value
