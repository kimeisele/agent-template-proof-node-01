"""Structured types for governance evaluation.

Every governance operation returns typed, testable results — never plain
strings or unstructured dicts.  Only the CLI layer formats these for
human display.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ComplianceStatus(Enum):
    """Overall governance compliance of a repository's default branch."""

    CONFORMANT = "conformant"          # Baseline fully satisfied
    NON_CONFORMANT = "non_conformant"  # Rules missing but fixable
    UNKNOWN = "unknown"                # Cannot be safely evaluated


class Diagnostic(Enum):
    """Diagnostic conditions that may accompany any compliance status."""

    OK = "ok"
    AUTH_MISSING = "auth_missing"
    PERMISSION_INSUFFICIENT = "permission_insufficient"
    REPO_NOT_FOUND = "repo_not_found"
    GITHUB_UNREACHABLE = "github_unreachable"
    API_ERROR = "api_error"
    UNSUPPORTED_CONFIG = "unsupported_config"


class RuleStatus(Enum):
    """Per-rule-type evaluation from two protection sources."""

    CONFIRMED = "confirmed"  # At least one reliably read source proves the rule
    MISSING = "missing"      # All relevant sources read; none prove the rule
    UNKNOWN = "unknown"      # No source proves it AND at least one source was unreadable


class BypassState(Enum):
    """Visibility of bypass actors on the default branch."""

    NONE_CONFIRMED = "none_confirmed"  # Positively confirmed: no bypasses visible
    PRESENT = "present"                # Bypass entries present in visible sources
    UNKNOWN = "unknown"                # Data not fully visible (e.g. 403 on detail endpoint)


@dataclass
class RepoInfo:
    """Repository identity confirmed via GitHub API."""

    full_name: str           # e.g. "kimeisele/my-node"
    default_branch: str      # remote-authoritative, from GET /repos/{owner}/{repo}


@dataclass
class GovernanceCheck:
    """Immutable result of reading and evaluating branch protection."""

    compliance: ComplianceStatus
    diagnostics: list[Diagnostic] = field(default_factory=list)
    repo_full_name: str | None = None
    default_branch: str | None = None
    rule_statuses: dict[str, RuleStatus] = field(default_factory=dict)
    present_rules: list[str] = field(default_factory=list)    # rule types that are CONFIRMED
    missing_rules: list[str] = field(default_factory=list)    # rule types that are MISSING
    unknown_rules: list[str] = field(default_factory=list)    # rule types that are UNKNOWN
    bypass_state: BypassState = BypassState.UNKNOWN
    details: list[str] = field(default_factory=list)          # human-readable diagnostic lines


@dataclass
class GovernanceResult:
    """Outcome of an ensure_governance_baseline() call."""

    check: GovernanceCheck                                  # state before action
    action: str | None = None                               # "created", "skipped", "skipped_conservative", None
    diagnostics: list[Diagnostic] = field(default_factory=list)  # apply-step diagnostics
    details: list[str] = field(default_factory=list)             # human-readable apply details
    final_check: GovernanceCheck | None = None              # re-read after action; must be CONFORMANT for success
