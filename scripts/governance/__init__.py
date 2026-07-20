"""Federation-Node-Governance — Basisschutz für den Default Branch.

Öffentliche API (3 Funktionen)::

    detect_repository(root) → (RepoInfo | None, Diagnostic)
    inspect_governance(repo) → GovernanceCheck
    ensure_governance_baseline(repo, check) → GovernanceResult

Alle GitHub-API-Zugriffe laufen über :func:`federation_utils.github_api`.
"""
from __future__ import annotations

from governance._models import (
    BypassState,
    ComplianceStatus,
    Diagnostic,
    GovernanceCheck,
    GovernanceResult,
    RepoInfo,
    RuleStatus,
)
from governance._protection import ensure_governance_baseline, inspect_governance
from governance._repo import detect_repository

__all__ = [
    "BypassState",
    "ComplianceStatus",
    "Diagnostic",
    "GovernanceCheck",
    "GovernanceResult",
    "RepoInfo",
    "RuleStatus",
    "detect_repository",
    "ensure_governance_baseline",
    "inspect_governance",
]
