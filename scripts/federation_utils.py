"""Shared utilities for federation scripts."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Structured GitHub API access ───────────────────────────────────────────

_API_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_API_BASE = "https://api.github.com"


@dataclass
class GitHubResponse:
    """Structured result of a GitHub API call.

    *status_code* is 0 for network / timeout errors where no HTTP
    response was received.
    """

    status_code: int
    body: dict[str, Any] | list[dict[str, Any]] | None
    error_message: str | None


def _resolve_token(token: str | None = None) -> str | None:
    """Resolve a GitHub token from the canonical cascade.

    1. explicit *token* parameter
    2. ``GITHUB_TOKEN`` environment variable
    3. ``GH_TOKEN`` environment variable
    4. ``gh auth token`` (subprocess, ignored on failure)
    """
    if token:
        return token
    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_token:
        return env_token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, FileNotFoundError):
        pass
    return None


def github_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str | None = None,
) -> GitHubResponse:
    """Make a GitHub REST API call via curl.

    *method*  — HTTP method (``GET``, ``POST``, …).
    *path*    — API path relative to ``https://api.github.com``,
                e.g. ``/repos/kimeisele/x/branches/main/protection``.
    *body*    — optional JSON request body.
    *token*   — optional explicit token; if ``None`` the canonical
                cascade is used (see :func:`_resolve_token`).

    Returns a :class:`GitHubResponse` with:

    * *status_code* — HTTP status (0 for network / timeout errors).
    * *body* — decoded JSON, or ``None`` on failure.
    * *error_message* — GitHub error message or curl / system error,
      ``None`` on success.
    """
    resolved = _resolve_token(token)
    cmd = [
        "curl", "-s", "-w", "%{http_code}",
        "--connect-timeout", "10",
        "-H", f"Accept: {_API_ACCEPT}",
        "-H", f"X-GitHub-Api-Version: {_API_VERSION}",
    ]
    if resolved:
        cmd += ["-H", f"Authorization: token {resolved}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    cmd += ["-X", method, f"{_API_BASE}{path}"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return GitHubResponse(
            status_code=0,
            body=None,
            error_message=f"curl error: {result.stderr.strip() or 'exit code ' + str(result.returncode)}",
        )

    stdout = result.stdout
    if len(stdout) < 3:
        return GitHubResponse(
            status_code=0,
            body=None,
            error_message="empty or truncated curl response",
        )

    # curl -w "%{http_code}" appends the status to stdout
    status_str = stdout[-3:]
    response_body = stdout[:-3]

    try:
        status_code = int(status_str)
    except ValueError:
        return GitHubResponse(
            status_code=0,
            body=None,
            error_message=f"could not parse HTTP status from curl output: {status_str!r}",
        )

    if not response_body.strip():
        return GitHubResponse(
            status_code=status_code,
            body=None,
            error_message=None if 200 <= status_code < 300 else f"HTTP {status_code}: empty response",
        )

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        return GitHubResponse(
            status_code=status_code,
            body=None,
            error_message=f"invalid JSON response: {exc}",
        )

    if 200 <= status_code < 300:
        return GitHubResponse(status_code=status_code, body=parsed, error_message=None)

    # GitHub error response — extract message
    if isinstance(parsed, dict):
        msg = parsed.get("message", f"HTTP {status_code}")
    else:
        msg = f"HTTP {status_code}"
    return GitHubResponse(status_code=status_code, body=parsed, error_message=str(msg))


# ── Legacy helpers (unchanged) ─────────────────────────────────────────────


def curl_json(url: str, token: str | None = None) -> dict | list | None:
    """Fetch JSON from *url* using curl.  Returns None on failure."""
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    cmd = ["curl", "-sf", "--connect-timeout", "10", "-H", "Accept: application/json"]
    if token:
        cmd += ["-H", f"Authorization: token {token}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def curl_bytes(url: str, token: str | None = None) -> bytes | None:
    """Fetch raw bytes from *url* using curl.  Returns None on failure."""
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    cmd = ["curl", "-sfL", "--connect-timeout", "10"]
    if token:
        cmd += ["-H", f"Authorization: token {token}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout


def display_name(repo_name: str) -> str:
    """Convert a repo name like 'my-cool-node' to 'My Cool Node'."""
    return " ".join(word.capitalize() for word in repo_name.replace("_", "-").split("-") if word) or repo_name


# ── Identity resolution ─────────────────────────────────────────────────────

# Stale template identity that must never be returned as a live default.
_TEMPLATE_REPO = "kimeisele/agent-template"


def repo_from_git_remote(repo_root: Path) -> str | None:
    """Extract ``owner/repo`` from the ``origin`` git remote.

    Returns ``None`` if no suitable GitHub remote exists.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=str(repo_root),
        )
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return _parse_github_full_name(result.stdout.strip())


def repo_from_setup_config(repo_root: Path) -> str | None:
    """Read the saved ``github_repo`` from ``.federation-setup.json``.

    Returns ``None`` if the file is missing, unreadable, or does not
    contain a ``github_repo`` key.
    """
    config_path = repo_root / ".federation-setup.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    repo = config.get("github_repo")
    return repo if isinstance(repo, str) and repo else None


def resolve_repo_identity(
    repo_root: Path | None = None,
    *,
    explicit_repo: str | None = None,
    _for_test: dict[str, str] | None = None,
) -> str:
    """Return the authoritative repository identity as ``owner/repo``.

    Resolution order (first successful source wins):

    1. *explicit_repo* — test/offline override.
    2. Git remote ``origin`` — parsed from the local checkout.
    3. ``.federation-setup.json`` ``github_repo`` field.
    4. ``GITHUB_REPOSITORY`` environment variable.
    5. **Fail** — ``RuntimeError``; no static fallback.

    The template default ``kimeisele/agent-template`` is **never**
    returned unless it is the actual git remote of the checkout.

    *explicit_repo* is validated as ``owner/name``.  An explicit value
    that matches the template identity is accepted (test scenarios) but
    logged as a warning.

    *repo_root* defaults to the parent of the ``scripts/`` directory
    (the repository root).
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]

    # 1. Explicit override (test/offline only)
    if explicit_repo:
        _validate_repo_format(explicit_repo)
        if explicit_repo == _TEMPLATE_REPO:
            print(
                f"warning: explicit --repo is the template identity "
                f"({_TEMPLATE_REPO}); descriptors will carry template "
                f"identity, not your node identity",
                file=sys.stderr,
            )
        return explicit_repo

    # 2. Git remote (authoritative for the local checkout)
    remote = repo_from_git_remote(repo_root)
    if remote:
        return remote

    # 3. Saved setup configuration
    config_repo = repo_from_setup_config(repo_root)
    if config_repo:
        if config_repo != _TEMPLATE_REPO:
            # Config has a non-template value — cross-check is
            # impossible without a git remote, so accept it.
            return config_repo
        print(
            "warning: saved config still references template identity "
            f"({_TEMPLATE_REPO}); has setup_node.py been run?",
            file=sys.stderr,
        )
        return config_repo

    # 4. Environment variable (GitHub Actions)
    env_repo = os.environ.get("GITHUB_REPOSITORY")
    if env_repo:
        _validate_repo_format(env_repo)
        if env_repo != _TEMPLATE_REPO:
            return env_repo
        print(
            "warning: GITHUB_REPOSITORY is the template identity "
            f"({_TEMPLATE_REPO}); descriptors will carry template identity",
            file=sys.stderr,
        )
        return env_repo

    # 5. Fail closed
    raise RuntimeError(
        "Cannot determine repository identity. "
        "Ensure this is a git repository with a GitHub remote, "
        "or set the GITHUB_REPOSITORY environment variable. "
        "For offline/test use, pass --repo explicitly."
    )


def _validate_repo_format(repo: str) -> None:
    """Raise ``ValueError`` if *repo* is not ``owner/name``."""
    if not re.fullmatch(r"[^/]+/[^/]+", repo):
        raise ValueError(
            f"Invalid repository format: {repo!r}. "
            f"Expected 'owner/repo' (e.g. 'kimeisele/my-node')."
        )


def _parse_github_full_name(remote_url: str) -> str | None:
    """Extract ``owner/repo`` from a git remote URL.

    Supports:
        - ``https://github.com/owner/repo.git``
        - ``git@github.com:owner/repo.git``
        - ``ssh://git@github.com/owner/repo.git``

    Trailing ``.git`` is stripped.  Returns ``None`` for non-GitHub URLs.
    """
    url = remote_url.rstrip("/")

    # https://github.com/owner/repo.git
    if "github.com/" in url:
        after = url.split("github.com/", 1)[1]
        name = after.removesuffix(".git").strip("/")
        parts = name.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None

    # git@github.com:owner/repo.git
    if "github.com:" in url:
        after = url.split("github.com:", 1)[1]
        name = after.removesuffix(".git").strip("/")
        parts = name.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None

    return None


# ── NADI path contract ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class NadiPathContract:
    """Resolved NADI file paths derived from a peer.json.

    The actual ``nadi-kit`` transport contract is::

        federation_dir = peer_path.parent
        outbox = federation_dir / "nadi_outbox.json"
        inbox  = federation_dir / "nadi_inbox.json"

    Declared ``nadi.outbox`` / ``nadi.inbox`` fields are validated
    against this contract and rejected if they differ.
    """

    peer_path: Path
    federation_dir: Path
    inbox_path: Path
    outbox_path: Path


class NadiPathError(ValueError):
    """Raised when the NADI path contract cannot be satisfied."""


def resolve_and_validate_nadi_paths(peer_path: Path) -> NadiPathContract:
    """Read *peer_path* and return a validated :class:`NadiPathContract`.

    Raises :class:`NadiPathError` on missing file, invalid JSON, or
    declarative path mismatches.

    This function is read-only — it does not create files, directories,
    or keys.
    """
    if not peer_path.exists():
        raise NadiPathError(f"peer.json not found: {peer_path}")

    try:
        peer = json.loads(peer_path.read_text())
    except json.JSONDecodeError as exc:
        raise NadiPathError(f"peer.json is not valid JSON: {exc}") from exc

    federation_dir = peer_path.parent
    # Repo root is two levels up from federation_dir:
    # peer.json at <repo>/data/federation/peer.json
    repo_root = federation_dir.parent.parent

    for key, filename in [("outbox", "nadi_outbox.json"),
                          ("inbox", "nadi_inbox.json")]:
        declared = peer.get("nadi", {}).get(key)
        if declared and isinstance(declared, str):
            resolved = (repo_root / declared).resolve()
            actual = (federation_dir / filename).resolve()
            if resolved != actual:
                raise NadiPathError(
                    f"nadi.{key} declares {declared} "
                    f"(resolves to {resolved}), "
                    f"but actual transport path is {actual}"
                )

    return NadiPathContract(
        peer_path=peer_path.resolve(),
        federation_dir=federation_dir.resolve(),
        inbox_path=(federation_dir / "nadi_inbox.json").resolve(),
        outbox_path=(federation_dir / "nadi_outbox.json").resolve(),
    )


# ── Human display name resolution ────────────────────────────────────────────


def resolve_human_display_name(repo_root: Path, repo_name: str) -> str:
    """Return the committed human-facing node name.

    Resolution order:
    1. ``docs/authority/capabilities.json`` ``display_name`` field
    2. slug-derived ``display_name(repo_name)``

    This function reads only committed, persistent files — not
    ``.federation-setup.json`` (which is gitignored).
    """
    manifest_path = repo_root / "docs" / "authority" / "capabilities.json"
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return display_name(repo_name)

    if not isinstance(data, dict):
        return display_name(repo_name)

    name = data.get("display_name")
    if isinstance(name, str) and name.strip():
        return name.strip()

    return display_name(repo_name)
