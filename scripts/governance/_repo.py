"""Repository detection from the local git checkout.

The default branch is always confirmed against the GitHub API;
local git information is never used as sole authority.
"""
from __future__ import annotations

from pathlib import Path

from federation_utils import github_api, repo_from_git_remote
from governance._models import Diagnostic, RepoInfo


def detect_repository(root: Path) -> tuple[RepoInfo | None, Diagnostic]:
    """Discover the GitHub repository this checkout belongs to.

    Extracts *owner/repo* from the ``origin`` git remote via
    :func:`federation_utils.repo_from_git_remote`, then confirms the
    default branch via ``GET /repos/{owner}/{repo}``.

    Returns:
        ``(RepoInfo, Diagnostic.OK)`` on success.
        ``(None, Diagnostic.REPO_NOT_FOUND)`` if no suitable git remote exists.
        ``(None, Diagnostic.GITHUB_UNREACHABLE)`` if the GitHub API is
        unreachable or the repository does not exist.
        ``(None, Diagnostic.AUTH_MISSING)`` on 401 from the API.
    """
    # ── 1. Extract owner/repo from git remote ──────────────────────────
    full_name = repo_from_git_remote(root)
    if full_name is None:
        return None, Diagnostic.REPO_NOT_FOUND

    # ── 2. Confirm default branch via GitHub API ───────────────────────
    response = github_api("GET", f"/repos/{full_name}")
    if response.status_code == 401:
        return None, Diagnostic.AUTH_MISSING
    if response.status_code == 404:
        return None, Diagnostic.REPO_NOT_FOUND
    if response.status_code == 0 or response.status_code >= 500:
        return None, Diagnostic.GITHUB_UNREACHABLE
    if not isinstance(response.body, dict):
        return None, Diagnostic.GITHUB_UNREACHABLE

    default_branch = response.body.get("default_branch")
    if not default_branch or not isinstance(default_branch, str):
        return None, Diagnostic.GITHUB_UNREACHABLE

    return RepoInfo(full_name=full_name, default_branch=default_branch), Diagnostic.OK
