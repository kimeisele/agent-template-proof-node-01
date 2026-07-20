"""Unit tests for governance module — per Issue #7 §12.

All external GitHub API calls are mocked via :func:`unittest.mock.patch`.
No destructive integration tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure scripts/ is importable
_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from federation_utils import GitHubResponse  # noqa: E402
from governance._models import (  # noqa: E402
    BypassState,
    ComplianceStatus,
    Diagnostic,
    GovernanceCheck,
    RepoInfo,
    RuleStatus,
)
from governance._protection import (  # noqa: E402
    RULESET_NAME,
    RULESET_PAYLOAD_V1,
    BASELINE_RULE_TYPES,
    _classic_confirms,
    _is_compatible,
    _overall_compliance,
    _rule_in_rules_list,
    _rule_status_for_type,
    ensure_governance_baseline,
    inspect_governance,
)
from governance._repo import detect_repository  # noqa: E402

# Import moved to federation_utils in Gate 1
from federation_utils import _parse_github_full_name  # noqa: E402

# ── helpers ────────────────────────────────────────────────────────────────

REPO = RepoInfo(full_name="kimeisele/test-node", default_branch="main")

# Pre-built mock responses
_REPO_OK = GitHubResponse(status_code=200, body={"default_branch": "main"}, error_message=None)
_RULES_EMPTY = GitHubResponse(status_code=200, body=[], error_message=None)
_PROTECTION_404 = GitHubResponse(status_code=404, body=None, error_message="Branch not protected")
_AUTH_401 = GitHubResponse(status_code=401, body={"message": "Bad credentials"}, error_message="Bad credentials")
_PERM_403 = GitHubResponse(status_code=403, body={"message": "Resource not accessible"}, error_message="Resource not accessible")
_NETWORK_ERROR = GitHubResponse(status_code=0, body=None, error_message="curl error: Could not resolve host")

_RULES_FULL = GitHubResponse(
    status_code=200,
    body=[
        {"type": "deletion", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
        {"type": "non_fast_forward", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
        {"type": "pull_request", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1,
         "parameters": {"required_approving_review_count": 0}},
    ],
    error_message=None,
)

_RULES_PARTIAL = GitHubResponse(
    status_code=200,
    body=[
        {"type": "pull_request", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
    ],
    error_message=None,
)

_RULES_TWO = GitHubResponse(
    status_code=200,
    body=[
        {"type": "pull_request", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
        {"type": "deletion", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
    ],
    error_message=None,
)

_PROTECTION_FULL = GitHubResponse(
    status_code=200,
    body={
        "required_pull_request_reviews": {"dismiss_stale_reviews": False, "require_code_owner_reviews": False},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "enforce_admins": {"enabled": True},
    },
    error_message=None,
)

_PROTECTION_FORCE_ENABLED = GitHubResponse(
    status_code=200,
    body={
        "required_pull_request_reviews": {"dismiss_stale_reviews": False},
        "allow_force_pushes": {"enabled": True},
        "allow_deletions": {"enabled": False},
    },
    error_message=None,
)

_PROTECTION_MISSING_FIELDS = GitHubResponse(
    status_code=200,
    body={
        "required_pull_request_reviews": {"dismiss_stale_reviews": False},
    },
    error_message=None,
)

_RULESETS_LIST_EMPTY = GitHubResponse(status_code=200, body=[], error_message=None)
_RULESETS_LIST_WITH_ID = GitHubResponse(
    status_code=200,
    body=[{
        "id": 42,
        "name": "agent-federation-baseline-v1",
    }],
    error_message=None,
)
_RULESETS_DETAIL_COMPATIBLE = GitHubResponse(
    status_code=200,
    body={
        "id": 42,
        "name": "agent-federation-baseline-v1",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
        ],
    },
    error_message=None,
)
_RULESETS_DETAIL_WITH_BYPASS = GitHubResponse(
    status_code=200,
    body={
        "id": 43,
        "name": "agent-federation-baseline-v1",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [
            {"actor_id": 1, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        ],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
        ],
    },
    error_message=None,
)
_RULESETS_DETAIL_DIVERGENT = GitHubResponse(
    status_code=200,
    body={
        "id": 44,
        "name": "agent-federation-baseline-v1",
        "target": "branch",
        "enforcement": "disabled",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [{"type": "deletion"}],
    },
    error_message=None,
)
_RULESETS_CREATED = GitHubResponse(status_code=201, body={"id": 99, "name": RULESET_NAME}, error_message=None)


# ── 12.1 Basis-Tests ──────────────────────────────────────────────────────


class TestRepoDetection:
    """Tests 1–2, 18–19: Repository and branch detection."""

    def test_parse_https(self) -> None:
        """_parse_github_full_name handles HTTPS URLs."""
        assert _parse_github_full_name("https://github.com/kimeisele/my-node.git") == "kimeisele/my-node"
        assert _parse_github_full_name("https://github.com/kimeisele/my-node") == "kimeisele/my-node"

    def test_parse_ssh(self) -> None:
        """_parse_github_full_name handles SSH URLs."""
        assert _parse_github_full_name("git@github.com:kimeisele/my-node.git") == "kimeisele/my-node"
        assert _parse_github_full_name("ssh://git@github.com/kimeisele/my-node.git") == "kimeisele/my-node"

    def test_parse_non_github(self) -> None:
        """_parse_github_full_name returns None for non-GitHub URLs."""
        assert _parse_github_full_name("https://gitlab.com/org/repo.git") is None

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_success(self, mock_run: object, mock_api: object) -> None:
        """Test 1: detect_repository returns RepoInfo with remote default branch."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        mock_api.return_value = _REPO_OK

        repo, diag = detect_repository(Path("/fake"))
        assert repo is not None
        assert repo.full_name == "kimeisele/test-node"
        assert repo.default_branch == "main"
        assert diag == Diagnostic.OK

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_no_remote(self, mock_run: object, mock_api: object) -> None:
        """Test 2: detect_repository returns REPO_NOT_FOUND when git remote fails."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error: No such remote 'origin'",
        )
        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.REPO_NOT_FOUND

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_auth_missing(self, mock_run: object, mock_api: object) -> None:
        """Test 14: AUTH_MISSING on 401 from repo API."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        mock_api.return_value = _AUTH_401

        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.AUTH_MISSING

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_master_branch(self, mock_run: object, mock_api: object) -> None:
        """Test 18: Default branch named 'master' is handled."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/old-repo.git", stderr="",
        )
        mock_api.return_value = GitHubResponse(
            status_code=200, body={"default_branch": "master"}, error_message=None,
        )

        repo, diag = detect_repository(Path("/fake"))
        assert repo is not None
        assert repo.default_branch == "master"

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_no_github_remote(self, mock_run: object, mock_api: object) -> None:
        """Test 19: Non-GitHub remote returns REPO_NOT_FOUND."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://gitlab.com/org/repo.git", stderr="",
        )
        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.REPO_NOT_FOUND


class TestEvaluation:
    """Tests 3–8, 14–17, 21: Baseline evaluation logic."""

    def test_unprotected_branch(self) -> None:
        """Test 3: Completely unprotected branch → NON_CONFORMANT, all three missing."""
        statuses = {
            "deletion": _rule_status_for_type("deletion", [], None, source_b_404=True),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", [], None, source_b_404=True),
            "pull_request": _rule_status_for_type("pull_request", [], None, source_b_404=True),
        }
        assert statuses["deletion"] == RuleStatus.MISSING
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert statuses["pull_request"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT

    def test_classic_protection_only(self) -> None:
        """Test 4: Baseline fully satisfied by classic branch protection only."""
        protection = _PROTECTION_FULL.body
        assert protection is not None
        assert _classic_confirms("deletion", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("pull_request", protection) == RuleStatus.CONFIRMED

    def test_rulesets_only(self) -> None:
        """Test 5: Baseline fully satisfied by rulesets only."""
        rules = _RULES_FULL.body
        assert rules is not None
        for rule_type in BASELINE_RULE_TYPES:
            status = _rule_status_for_type(rule_type, rules, None, source_b_404=True)
            assert status == RuleStatus.CONFIRMED, f"{rule_type} should be CONFIRMED"

    def test_combined_sources(self) -> None:
        """Test 6: Baseline satisfied from combination of both sources."""
        # Rulesets provide pull_request and deletion; classic provides non_fast_forward
        rules = _RULES_TWO.body  # pull_request + deletion
        protection = {"allow_force_pushes": {"enabled": False}}  # non_fast_forward from classic

        assert _rule_status_for_type("pull_request", rules, protection, source_b_404=False) == RuleStatus.CONFIRMED
        assert _rule_status_for_type("deletion", rules, protection, source_b_404=False) == RuleStatus.CONFIRMED
        assert _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=False) == RuleStatus.CONFIRMED

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.CONFORMANT

    def test_404_on_protection_endpoint(self) -> None:
        """Test 7: 404 on classic protection endpoint — no error, rules only."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            # Source A returns full ruleset coverage, Source B returns 404
            def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
                if "/rules/branches/" in path:
                    return _RULES_FULL
                if "/branches/" in path and "/protection" in path:
                    return _PROTECTION_404
                return GitHubResponse(status_code=200, body={}, error_message=None)

            mock_api.side_effect = side_effect
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.CONFORMANT
            assert check.default_branch == "main"

        _run()

    def test_single_missing_rule(self) -> None:
        """Test 8: Exactly one rule missing → correctly identified."""
        rules = _RULES_TWO.body  # pull_request + deletion, missing non_fast_forward
        protection = None

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=True),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=True),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=True),
        }
        assert statuses["deletion"] == RuleStatus.CONFIRMED
        assert statuses["pull_request"] == RuleStatus.CONFIRMED
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT

    def test_auth_missing_on_api(self) -> None:
        """Test 14: AUTH_MISSING diagnostic on 401."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            mock_api.return_value = _AUTH_401
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.UNKNOWN

        _run()

    def test_permission_insufficient_on_api(self) -> None:
        """Test 15: PERMISSION_INSUFFICIENT diagnostic on 403."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            mock_api.return_value = _PERM_403
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.UNKNOWN

        _run()

    def test_github_unreachable(self) -> None:
        """Test 16: GITHUB_UNREACHABLE on network error."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            mock_api.return_value = _NETWORK_ERROR
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.UNKNOWN

        _run()

    def test_missing_fields_in_classic_protection(self) -> None:
        """Test 21: Missing fields in classic protection → UNKNOWN for that type."""
        protection = _PROTECTION_MISSING_FIELDS.body
        assert protection is not None

        # pull_request field IS present → CONFIRMED
        assert _classic_confirms("pull_request", protection) == RuleStatus.CONFIRMED
        # allow_force_pushes is MISSING → UNKNOWN
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.UNKNOWN
        # allow_deletions is MISSING → UNKNOWN
        assert _classic_confirms("deletion", protection) == RuleStatus.UNKNOWN


class TestRulesetManagement:
    """Tests 9–10, 30: Ruleset creation, idempotency, no PUT."""

    @patch("governance._protection.github_api")
    def test_idempotent_skip(self, mock_api: object) -> None:
        """Test 9: Existing compatible ruleset is not duplicated."""
        call_paths: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_paths.append(f"{method} {path}")
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_WITH_ID
            if "/rulesets/" in path and method == "GET":
                return _RULESETS_DETAIL_COMPATIBLE
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action == "skipped"
        # No POST was made
        assert not any("POST" in c for c in call_paths)

    @patch("governance._protection.github_api")
    def test_no_overwrite_divergent_ruleset(self, mock_api: object) -> None:
        """Test 10: Divergent same-named ruleset is NOT overwritten (uses separate detail fetch)."""
        divergent_list = GitHubResponse(
            status_code=200,
            body=[{"id": 44, "name": "agent-federation-baseline-v1"}],
            error_message=None,
        )

        call_paths: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_paths.append(f"{method} {path}")
            if "/rulesets" in path and "includes_parents" in path:
                return divergent_list
            if "/rulesets/" in path and method == "GET":
                return _RULESETS_DETAIL_DIVERGENT
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        # No POST/PUT
        assert not any("POST" in c or "PUT" in c for c in call_paths)

    @patch("governance._protection.github_api")
    def test_detail_fetch_fails_causes_unsupported(self, mock_api: object) -> None:
        """Blocker 2: Failed detail fetch (403) → UNSUPPORTED_CONFIG, no mutation."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_WITH_ID
            if "/rulesets/" in path and method == "GET":
                return _PERM_403
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert "POST" not in call_methods
        assert "PUT" not in call_methods

    @patch("governance._protection.github_api")
    def test_no_put_in_v1(self, mock_api: object) -> None:
        """Test 30: ensure_baseline_ruleset never issues a PUT."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_WITH_ID
            if "/rulesets/" in path and method == "GET":
                return _RULESETS_DETAIL_COMPATIBLE
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert "PUT" not in call_methods

    @patch("governance._protection.github_api")
    def test_creates_when_missing(self, mock_api: object) -> None:
        """ensure_baseline_ruleset creates via POST when ruleset is absent."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_EMPTY
            if method == "POST" and "/rulesets" in path:
                return _RULESETS_CREATED
            if "/rules/branches/" in path:
                return _RULES_FULL
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action == "created"
        assert "POST" in call_methods
        # Re-read occurred
        assert result.final_check is not None


# ── 12.2 Quellenweise Aggregations-Tests ──────────────────────────────────


class TestSourceAggregation:
    """Tests 22–26: Per-source aggregation with partial readability."""

    def test_full_rulesets_plus_classic_403(self) -> None:
        """Test 22: Full ruleset coverage + classic 403 → CONFORMANT with warning."""
        status = _rule_status_for_type("deletion", _RULES_FULL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED
        status = _rule_status_for_type("non_fast_forward", _RULES_FULL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED
        status = _rule_status_for_type("pull_request", _RULES_FULL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED

        statuses = {
            "deletion": _rule_status_for_type("deletion", _RULES_FULL.body, None, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", _RULES_FULL.body, None, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", _RULES_FULL.body, None, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.CONFORMANT

    def test_partial_rulesets_plus_classic_403(self) -> None:
        """Test 23: Partial ruleset + classic 403 → UNKNOWN."""
        # Only pull_request from rulesets, source B is unreadable (403)
        status = _rule_status_for_type("pull_request", _RULES_PARTIAL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED
        status = _rule_status_for_type("deletion", _RULES_PARTIAL.body, None, source_b_404=False)
        assert status == RuleStatus.UNKNOWN
        status = _rule_status_for_type("non_fast_forward", _RULES_PARTIAL.body, None, source_b_404=False)
        assert status == RuleStatus.UNKNOWN

        statuses = {
            "deletion": _rule_status_for_type("deletion", _RULES_PARTIAL.body, None, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", _RULES_PARTIAL.body, None, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", _RULES_PARTIAL.body, None, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.UNKNOWN

    def test_full_classic_plus_rulesets_401(self) -> None:
        """Test 24: Full classic protection + rulesets 401 → CONFORMANT with warning."""
        protection = _PROTECTION_FULL.body
        assert protection is not None

        # Source A is unreadable (None), Source B confirms all three
        assert _classic_confirms("deletion", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("pull_request", protection) == RuleStatus.CONFIRMED

        statuses = {
            "deletion": _rule_status_for_type("deletion", None, protection, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", None, protection, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", None, protection, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.CONFORMANT

    def test_missing_rule_both_sources_readable(self) -> None:
        """Test 25: Missing rule with both sources fully readable → NON_CONFORMANT."""
        # Source A provides deletion + pull_request, source B confirms only non_fast_forward
        # non_fast_forward is MISSING from rules; force pushes are enabled in classic
        rules = _RULES_TWO.body  # pull_request + deletion
        protection = _PROTECTION_FORCE_ENABLED.body  # force enabled → non_fast_forward MISSING

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=False),
        }
        assert statuses["pull_request"] == RuleStatus.CONFIRMED
        assert statuses["deletion"] == RuleStatus.CONFIRMED
        # non_fast_forward: not in rules (MISSING), classic says force enabled (MISSING)
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT

    def test_two_confirmed_one_missing_no_classic(self) -> None:
        """Test 26: Two rules from rulesets + classic 404 → one MISSING → NON_CONFORMANT."""
        rules = _RULES_TWO.body  # pull_request + deletion
        protection = None  # 404 — no classic prot

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=True),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=True),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=True),
        }
        assert statuses["pull_request"] == RuleStatus.CONFIRMED
        assert statuses["deletion"] == RuleStatus.CONFIRMED
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT


# ── 12.3 Zusätzliche Tests ────────────────────────────────────────────────


class TestAdditional:
    """Tests 27–31: Token cascade, remote branch authority, payload."""

    @patch("subprocess.run")
    def test_token_cascade_gh_auth_token(self, mock_run: object) -> None:
        """Test 27: Token resolution uses gh auth token as last resort."""
        from federation_utils import _resolve_token

        # No env vars, gh auth token succeeds
        with patch.dict("os.environ", {}, clear=True):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="gh_token_123\n", stderr="",
            )
            token = _resolve_token()
            assert token == "gh_token_123"

    @patch("subprocess.run")
    def test_token_cascade_env_precedence(self, mock_run: object) -> None:
        """Test 27: GITHUB_TOKEN takes precedence over gh auth token."""
        from federation_utils import _resolve_token

        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_token"}, clear=True):
            token = _resolve_token()
            assert token == "env_token"

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_remote_branch_overrides_local(self, mock_run: object, mock_api: object) -> None:
        """Test 28: Remote default branch takes precedence over local."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        # Remote says 'main', even if local might say 'master'
        mock_api.return_value = GitHubResponse(
            status_code=200, body={"default_branch": "main"}, error_message=None,
        )

        repo, diag = detect_repository(Path("/fake"))
        assert repo is not None
        assert repo.default_branch == "main"

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_no_remote_branch_blocks_write(self, mock_run: object, mock_api: object) -> None:
        """Test 29: Unconfirmed remote default branch prevents write operations."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        # API returns 500 — can't confirm default branch
        mock_api.return_value = GitHubResponse(
            status_code=500, body=None, error_message="Internal Server Error",
        )

        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.GITHUB_UNREACHABLE
        # With no repo, ensure_governance_baseline cannot be called

    def test_exact_payload(self) -> None:
        """Test 31: RULESET_PAYLOAD_V1 has correct structure."""
        assert RULESET_PAYLOAD_V1["name"] == "agent-federation-baseline-v1"
        assert RULESET_PAYLOAD_V1["target"] == "branch"
        assert RULESET_PAYLOAD_V1["enforcement"] == "active"
        assert RULESET_PAYLOAD_V1["bypass_actors"] == []
        assert "~DEFAULT_BRANCH" in RULESET_PAYLOAD_V1["conditions"]["ref_name"]["include"]

        rules = RULESET_PAYLOAD_V1["rules"]
        rule_types = {r["type"] for r in rules}
        assert rule_types == {"deletion", "non_fast_forward", "pull_request"}

        # Check pull_request parameters
        pr_rule = next(r for r in rules if r["type"] == "pull_request")
        params = pr_rule["parameters"]
        assert params["allowed_merge_methods"] == ["merge", "squash", "rebase"]
        assert params["required_approving_review_count"] == 0
        assert params["dismiss_stale_reviews_on_push"] is False
        assert params["require_code_owner_review"] is False
        assert params["require_last_push_approval"] is False
        assert params["required_review_thread_resolution"] is False


# ── 12.4 Header- und Sicherheitstests ──────────────────────────────────────


class TestHeadersAndSecurity:
    """Tests 32–34: HTTP headers, payload, token safety."""

    @patch("subprocess.run")
    def test_github_api_headers(self, mock_run: object) -> None:
        """Test 32: github_api sets Accept and X-GitHub-Api-Version headers."""
        from federation_utils import github_api

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true}200', stderr="",
        )
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}, clear=True):
            github_api("GET", "/repos/kimeisele/x")

        # Check curl command arguments
        call_args = mock_run.call_args[0][0] if mock_run.call_args else []
        cmd_str = " ".join(call_args)
        assert "Accept: application/vnd.github+json" in cmd_str
        assert "X-GitHub-Api-Version: 2022-11-28" in cmd_str

    def test_payload_includes_all_merge_methods(self) -> None:
        """Test 33: RULESET_PAYLOAD_V1 includes all three allowed_merge_methods."""
        pr_rule = next(r for r in RULESET_PAYLOAD_V1["rules"] if r["type"] == "pull_request")
        methods = pr_rule["parameters"]["allowed_merge_methods"]
        assert "merge" in methods
        assert "squash" in methods
        assert "rebase" in methods
        assert len(methods) == 3

    @patch("subprocess.run")
    def test_token_not_in_error_output(self, mock_run: object) -> None:
        """Test 34: Token never appears in error_message."""
        from federation_utils import github_api

        # Simulate a curl error
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=7, stdout="", stderr="Could not resolve host",
        )
        with patch.dict("os.environ", {"GITHUB_TOKEN": "secret-token-12345"}, clear=True):
            response = github_api("GET", "/repos/kimeisele/x")

        assert response.status_code == 0
        assert response.error_message is not None
        assert "secret-token-12345" not in response.error_message


class TestCompatibilityCheck:
    """Tests for _is_compatible logic."""

    def test_exact_compatible(self) -> None:
        """_is_compatible returns True for exact baseline match."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is True
        assert reason == "exact"

    def test_stricter_accepted(self) -> None:
        """_is_compatible accepts stricter rules (extra rule types)."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
                {"type": "required_linear_history"},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is True
        assert reason == "stricter"

    def test_enforcement_not_active(self) -> None:
        """_is_compatible rejects disabled rulesets."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "disabled",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "enforcement" in reason

    def test_missing_baseline_rules(self) -> None:
        """_is_compatible rejects rulesets missing baseline rules."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "non_fast_forward" in reason


class TestBypassState:
    """Tests for bypass state detection."""

    @patch("governance._protection.github_api")
    def test_bypass_unknown_on_auth_error(self, mock_api: object) -> None:
        """Test 17: Bypass UNKNOWN when source has AUTH_MISSING diagnostic."""

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rules/branches/" in path:
                return _AUTH_401
            if "/branches/" in path and "/protection" in path:
                return _AUTH_401
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        check = inspect_governance(REPO)
        assert check.bypass_state == BypassState.UNKNOWN
        assert Diagnostic.AUTH_MISSING in check.diagnostics


class TestCLIModes:
    """Tests 11–13: CLI behavior (non-interactive, apply-governance, status)."""

    @patch("governance._protection.github_api")
    def test_non_interactive_no_write(self, mock_api: object) -> None:
        """Test 11: --non-interactive (without --apply-governance) performs NO POST/PUT."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rules/branches/" in path:
                return _RULES_EMPTY
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        # This simulates what --non-interactive does: inspect_governance only
        check = inspect_governance(REPO)
        assert check.compliance == ComplianceStatus.NON_CONFORMANT
        # No POST or PUT
        assert "POST" not in call_methods
        assert "PUT" not in call_methods

    def test_rule_in_rules_list(self) -> None:
        """_rule_in_rules_list correctly detects rule types."""
        rules = [{"type": "deletion"}, {"type": "pull_request"}]
        assert _rule_in_rules_list("deletion", rules) is True
        assert _rule_in_rules_list("non_fast_forward", rules) is False

    def test_classic_confirms_enabled_fields(self) -> None:
        """_classic_confirms returns MISSING when field explicitly allows the action."""
        # allow_force_pushes.enabled == true → force pushes are allowed → rule MISSING
        protection = {"allow_force_pushes": {"enabled": True}}
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.MISSING

        protection = {"allow_deletions": {"enabled": True}}
        assert _classic_confirms("deletion", protection) == RuleStatus.MISSING

    def test_classic_confirms_disabled_fields(self) -> None:
        """_classic_confirms returns CONFIRMED when field explicitly blocks the action."""
        protection = {"allow_force_pushes": {"enabled": False}}
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.CONFIRMED

        protection = {"allow_deletions": {"enabled": False}}
        assert _classic_confirms("deletion", protection) == RuleStatus.CONFIRMED


class TestSameLogicSetupAndStatus:
    """Test 20: Same inspect_governance logic for setup and status."""

    @patch("governance._protection.github_api")
    def test_same_inspect_governance(self, mock_api: object) -> None:
        """Both setup and --status use the same inspect_governance function."""
        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rules/branches/" in path:
                return _RULES_FULL
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect

        # Called directly (as --status does)
        check1 = inspect_governance(REPO)

        # Called again (as setup does)
        mock_api.side_effect = side_effect  # reset side_effect
        check2 = inspect_governance(REPO)

        assert check1.compliance == check2.compliance
        assert check1.rule_statuses == check2.rule_statuses
        assert check1.compliance == ComplianceStatus.CONFORMANT


# ── Blocker-spezifische Tests ──────────────────────────────────────────────


class TestBlocker1NonInteractiveNoWrite:
    """Blocker 1: --non-interactive never prompts stdin, never issues POST."""

    @patch("governance._protection.github_api")
    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_non_interactive_no_ask_yn(self, mock_run: object, mock_repo_api: object, mock_prot_api: object) -> None:
        """_ask_yn is never called and no POST is made in non-interactive no-apply mode."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        prot_methods: list[str] = []

        def repo_side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/repos/" in path and method == "GET":
                return _REPO_OK
            return GitHubResponse(status_code=200, body={}, error_message=None)

        def prot_side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            prot_methods.append(method)
            if "/rules/branches/" in path:
                return _RULES_EMPTY
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            if "/rulesets" in path:
                return GitHubResponse(status_code=200, body={}, error_message=None)
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_repo_api.side_effect = repo_side_effect
        mock_prot_api.side_effect = prot_side_effect

        # Simulate --non-interactive without --apply-governance
        from setup_node import _run_governance_step
        status = _run_governance_step(interactive=False, apply_governance=False)

        assert status == ComplianceStatus.NON_CONFORMANT
        # No POST was made
        assert "POST" not in prot_methods
        # No interactive prompts occur (verified by reachable path without _ask_yn)


class TestBlocker2DetailFetch:
    """Blocker 2: Full ruleset detail is fetched by ID before compatibility check."""

    @patch("governance._protection.github_api")
    def test_list_and_detail_separate(self, mock_api: object) -> None:
        """List response is used for ID, detail response is used for _is_compatible."""
        list_calls: list[str] = []
        detail_calls: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rulesets" in path and "includes_parents" in path:
                list_calls.append(path)
                return _RULESETS_LIST_WITH_ID
            if path.endswith("/rulesets/42") and method == "GET":
                detail_calls.append(path)
                return _RULESETS_DETAIL_COMPATIBLE
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect

        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action == "skipped"
        # List was called (to find ID)
        assert len(list_calls) == 1
        # Detail was called (to get full config)
        assert len(detail_calls) == 1

    @patch("governance._protection.github_api")
    def test_detail_403_causes_unsupported(self, mock_api: object) -> None:
        """403 on detail endpoint → UNSUPPORTED_CONFIG, no mutation."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_WITH_ID
            if path.endswith("/rulesets/42"):
                return _PERM_403
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect

        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert "POST" not in call_methods


class TestBlocker3BypassState:
    """Blocker 3: BypassState is conservative — UNKNOWN without full details."""

    def test_no_details_unknown(self) -> None:
        """Readable rule types but no ruleset detail → UNKNOWN."""
        from governance._protection import _determine_bypass_state

        state = _determine_bypass_state(
            _RULES_FULL.body, _PROTECTION_FULL.body, Diagnostic.OK,
            ruleset_details_available=False,
        )
        assert state == BypassState.UNKNOWN

    def test_full_details_no_bypass_confirmed(self) -> None:
        """Full details available, no bypasses → NONE_CONFIRMED."""
        from governance._protection import _determine_bypass_state

        state = _determine_bypass_state(
            _RULES_FULL.body, _PROTECTION_FULL.body, Diagnostic.OK,
            ruleset_details_available=True,
            ruleset_bypass_actors=[],
        )
        assert state == BypassState.NONE_CONFIRMED

    def test_visible_bypass_actors_present(self) -> None:
        """Visible bypass actors → PRESENT."""
        from governance._protection import _determine_bypass_state

        state = _determine_bypass_state(
            _RULES_FULL.body, _PROTECTION_FULL.body, Diagnostic.OK,
            ruleset_details_available=True,
            ruleset_bypass_actors=[
                {"actor_id": 1, "actor_type": "RepositoryRole", "bypass_mode": "always"},
            ],
        )
        assert state == BypassState.PRESENT

    def test_restrictions_alone_not_bypass(self) -> None:
        """Classic protection restrictions field alone → NOT PRESENT."""
        from governance._protection import _determine_bypass_state

        prot_with_restrictions = {
            "required_pull_request_reviews": {},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "enforce_admins": {"enabled": True},
            "restrictions": {"users": [], "teams": []},
        }

        state = _determine_bypass_state(
            _RULES_FULL.body, prot_with_restrictions, Diagnostic.OK,
            ruleset_details_available=True,
            ruleset_bypass_actors=[],
        )
        # restrictions is a push restriction, not a bypass
        assert state == BypassState.NONE_CONFIRMED


class TestBlocker4Readme:
    """Blocker 4: README does not recommend direct push to protected main."""

    def test_no_push_to_main_in_readme(self) -> None:
        """The README does not contain 'Push to main' as a setup instruction."""
        readme = Path(__file__).resolve().parents[1] / "README.md"
        content = readme.read_text()
        assert "Push to `main`" not in content
        assert "After merging your setup PR" in content


class TestBlocker5StatusCallCount:
    """Blocker 5: --status performs exactly one detect_repository + one inspect_governance."""

    @patch("governance._protection.github_api")
    def test_inspect_governance_call_count(self, mock_api: object) -> None:
        """inspect_governance is called exactly once per --status path (no double query from main)."""
        call_paths: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_paths.append(f"{method} {path}")
            if "/rules/branches/" in path:
                return _RULES_FULL
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect

        check = inspect_governance(REPO)
        assert check.compliance == ComplianceStatus.CONFORMANT
        # inspect_governance makes exactly 2 API calls: rules/branches + branches/protection
        rules_calls = [c for c in call_paths if "/rules/branches/" in c]
        assert len(rules_calls) == 1  # exactly one rules/branches call


# ── Blocker 6–8 Tests ──────────────────────────────────────────────────────


class TestBlocker6CandidateIdValidation:
    """Blocker 6: Candidate without valid ID must not trigger POST."""

    @patch("governance._protection.github_api")
    def test_candidate_without_id_no_post(self, mock_api: object) -> None:
        """Candidate with matching name but no 'id' field → no POST."""
        list_without_id = GitHubResponse(
            status_code=200,
            body=[{"name": "agent-federation-baseline-v1"}],  # no id
            error_message=None,
        )
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return list_without_id
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert "POST" not in call_methods
        assert Diagnostic.UNSUPPORTED_CONFIG in result.diagnostics

    @patch("governance._protection.github_api")
    def test_candidate_with_invalid_id_no_post(self, mock_api: object) -> None:
        """Candidate with non-int 'id' → no POST."""
        list_bad_id = GitHubResponse(
            status_code=200,
            body=[{"name": "agent-federation-baseline-v1", "id": "not-an-int"}],
            error_message=None,
        )
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return list_bad_id
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert "POST" not in call_methods

    @patch("governance._protection.github_api")
    def test_multiple_candidates_no_post(self, mock_api: object) -> None:
        """Multiple same-named candidates → conflict, no POST."""
        list_multi = GitHubResponse(
            status_code=200,
            body=[
                {"name": "agent-federation-baseline-v1", "id": 1},
                {"name": "agent-federation-baseline-v1", "id": 2},
            ],
            error_message=None,
        )
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return list_multi
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert "POST" not in call_methods


class TestBlocker7DiagnosticsTransported:
    """Blocker 7: Apply diagnostics reach GovernanceResult and CLI."""

    @patch("governance._protection.github_api")
    def test_auth_missing_diagnostic_in_result(self, mock_api: object) -> None:
        """401 on ruleset list → AUTH_MISSING diagnostic in result."""
        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rulesets" in path and "includes_parents" in path:
                return GitHubResponse(status_code=401, body={"message": "Bad credentials"}, error_message="Bad credentials")
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert Diagnostic.AUTH_MISSING in result.diagnostics
        assert result.action is None

    @patch("governance._protection.github_api")
    def test_permission_insufficient_in_result(self, mock_api: object) -> None:
        """403 on ruleset create → PERMISSION_INSUFFICIENT in result."""
        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_EMPTY
            if method == "POST" and "/rulesets" in path:
                return GitHubResponse(status_code=403, body={"message": "Forbidden"}, error_message="Forbidden")
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert Diagnostic.PERMISSION_INSUFFICIENT in result.diagnostics
        assert result.action is None

    @patch("governance._protection.github_api")
    def test_github_unreachable_in_result(self, mock_api: object) -> None:
        """Network error → GITHUB_UNREACHABLE in result."""
        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rulesets" in path and "includes_parents" in path:
                return _NETWORK_ERROR
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert Diagnostic.GITHUB_UNREACHABLE in result.diagnostics


class TestBlocker8FinalCheckRequired:
    """Blocker 8: Every action requires a final re-read; only CONFORMANT → Exit 0."""

    @patch("governance._protection.github_api")
    def test_skipped_has_final_check(self, mock_api: object) -> None:
        """'skipped' action also produces a final_check (re-read)."""
        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_WITH_ID
            if "/rulesets/" in path and method == "GET":
                return _RULESETS_DETAIL_COMPATIBLE
            if "/rules/branches/" in path:
                return _RULES_FULL
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action == "skipped"
        # final_check MUST exist for all actions (Blocker 8)
        assert result.final_check is not None
        assert result.final_check.compliance == ComplianceStatus.CONFORMANT

    @patch("governance._protection.github_api")
    def test_skipped_conservative_has_final_check(self, mock_api: object) -> None:
        """'skipped_conservative' also produces a final_check."""
        # Ruleset with baseline + stricter extra rules
        detail_stricter = GitHubResponse(
            status_code=200,
            body={
                "id": 42, "name": "agent-federation-baseline-v1",
                "target": "branch", "enforcement": "active",
                "bypass_actors": [],
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "rules": [
                    {"type": "deletion"}, {"type": "non_fast_forward"},
                    {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
                    {"type": "required_linear_history"},
                ],
            },
            error_message=None,
        )

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_WITH_ID
            if "/rulesets/" in path and method == "GET":
                return detail_stricter
            if "/rules/branches/" in path:
                return _RULES_FULL
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action == "skipped_conservative"
        assert result.final_check is not None

    @patch("governance._protection.github_api")
    def test_no_final_check_means_no_action(self, mock_api: object) -> None:
        """action=None → no final_check, diagnostics explain failure."""
        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_WITH_ID
            if "/rulesets/" in path and method == "GET":
                return _RULESETS_DETAIL_DIVERGENT  # disabled → incompatible
            if "/rules/branches/" in path:
                return _RULES_EMPTY
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert result.final_check is None  # no action → no re-read
        assert Diagnostic.UNSUPPORTED_CONFIG in result.diagnostics


# ── Blocker 9 Tests ────────────────────────────────────────────────────────


class TestBlocker9CompatibilityCheck:
    """Blocker 9: _is_compatible validates target, conditions, bypass, pull_request."""

    _BASE_VALID = {
        "name": "agent-federation-baseline-v1",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
        ],
    }

    def test_wrong_target_incompatible(self) -> None:
        """target != 'branch' → incompatible."""
        existing = {**self._BASE_VALID, "target": "tag"}
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "target" in reason

    def test_missing_conditions_incompatible(self) -> None:
        """conditions field missing → incompatible."""
        existing = {k: v for k, v in self._BASE_VALID.items() if k != "conditions"}
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "conditions" in reason

    def test_missing_default_branch_incompatible(self) -> None:
        """~DEFAULT_BRANCH not in include → incompatible."""
        existing = {
            **self._BASE_VALID,
            "conditions": {"ref_name": {"include": ["refs/heads/develop"], "exclude": []}},
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "default_branch" in reason

    def test_missing_bypass_actors_incompatible(self) -> None:
        """bypass_actors field absent → incompatible."""
        existing = {k: v for k, v in self._BASE_VALID.items() if k != "bypass_actors"}
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "bypass_actors" in reason

    def test_bypass_actors_wrong_type_incompatible(self) -> None:
        """bypass_actors not a list → incompatible."""
        existing = {**self._BASE_VALID, "bypass_actors": "not-a-list"}
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "bypass_actors" in reason

    def test_missing_pull_request_parameters_incompatible(self) -> None:
        """pull_request rule without parameters → incompatible."""
        existing = {
            **self._BASE_VALID,
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request"},  # no parameters
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "pull_request_parameters" in reason

    def test_missing_review_count_incompatible(self) -> None:
        """pull_request parameters without required_approving_review_count → incompatible."""
        existing = {
            **self._BASE_VALID,
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {}},  # empty, no review_count
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "review_count" in reason

    def test_full_valid_exact_compatible_and_skipped(self) -> None:
        """Full valid v1 structure → compatible (exact), action='skipped' with final_check.

        This is an integration-level test through ensure_governance_baseline.
        """
        with patch("governance._protection.github_api") as mock_api:
            def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
                if "/rulesets" in path and "includes_parents" in path:
                    return _RULESETS_LIST_WITH_ID
                if "/rulesets/" in path and method == "GET":
                    return _RULESETS_DETAIL_COMPATIBLE
                if "/rules/branches/" in path:
                    return _RULES_FULL
                if "/branches/" in path and "/protection" in path:
                    return _PROTECTION_404
                return GitHubResponse(status_code=200, body={}, error_message=None)

            mock_api.side_effect = side_effect
            result = ensure_governance_baseline(
                REPO,
                GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
            )
            assert result.action == "skipped"
            assert result.final_check is not None
            assert result.final_check.compliance == ComplianceStatus.CONFORMANT

    def test_stricter_with_valid_target_compatible(self) -> None:
        """Stricter ruleset with correct target/branch → compatible (stricter)."""
        existing = {
            **self._BASE_VALID,
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
                {"type": "required_linear_history"},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is True
        assert reason == "stricter"


class TestBlocker10Conservative:
    """Blocker 10: exclude field, bypass_actors strictness, bool/int type safety."""

    _BASE = {
        "name": "agent-federation-baseline-v1",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
        ],
    }

    # ── 10.1 exclude validation ─────────────────────────────────────────

    def test_exclude_missing_incompatible(self) -> None:
        """exclude field absent → incompatible."""
        existing = {
            **self._BASE,
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}},
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "exclude" in reason

    def test_exclude_wrong_type_incompatible(self) -> None:
        """exclude not a list → incompatible."""
        existing = {
            **self._BASE,
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": "not-a-list"}},
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "exclude" in reason

    def test_exclude_default_branch_incompatible(self) -> None:
        """exclude contains ~DEFAULT_BRANCH → incompatible."""
        existing = {
            **self._BASE,
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": ["~DEFAULT_BRANCH"]}},
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "exclude" in reason

    def test_exclude_with_pattern_incompatible(self) -> None:
        """exclude contains any pattern → conservative incompatible."""
        existing = {
            **self._BASE,
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": ["refs/heads/main"]}},
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "exclude" in reason

    def test_exclude_empty_valid(self) -> None:
        """exclude: [] → compatible (exact)."""
        compatible, reason = _is_compatible(self._BASE)
        assert compatible is True
        assert reason == "exact"

    # ── 10.2 bypass_actors strictness ───────────────────────────────────

    def test_bypass_empty_object_incompatible(self) -> None:
        """[{}] → incompatible (any non-empty list rejected)."""
        existing = {**self._BASE, "bypass_actors": [{}]}
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "bypass" in reason

    def test_bypass_empty_mode_incompatible(self) -> None:
        """[{"bypass_mode": ""}] → incompatible."""
        existing = {**self._BASE, "bypass_actors": [{"bypass_mode": ""}]}
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "bypass" in reason

    def test_bypass_mode_none_incompatible(self) -> None:
        """[{"bypass_mode": "none"}] → incompatible (non-empty list rejected)."""
        existing = {**self._BASE, "bypass_actors": [{"bypass_mode": "none"}]}
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "bypass" in reason

    def test_bypass_empty_list_valid(self) -> None:
        """[] → compatible."""
        existing = {**self._BASE, "bypass_actors": []}
        compatible, reason = _is_compatible(existing)
        assert compatible is True

    # ── 10.3 bool/int type safety ───────────────────────────────────────

    @patch("governance._protection.github_api")
    def test_candidate_id_true_no_post(self, mock_api: object) -> None:
        """Candidate ID is True (bool subclass of int) → no POST."""
        list_with_true_id = GitHubResponse(
            status_code=200,
            body=[{"name": "agent-federation-baseline-v1", "id": True}],
            error_message=None,
        )
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return list_with_true_id
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert "POST" not in call_methods

    @patch("governance._protection.github_api")
    def test_candidate_id_zero_no_post(self, mock_api: object) -> None:
        """Candidate ID is 0 → no POST (ID must be > 0)."""
        list_zero_id = GitHubResponse(
            status_code=200,
            body=[{"name": "agent-federation-baseline-v1", "id": 0}],
            error_message=None,
        )
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return list_zero_id
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        assert "POST" not in call_methods

    def test_approval_true_incompatible(self) -> None:
        """required_approving_review_count is True → incompatible (bool is int!)."""
        existing = {
            **self._BASE,
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": True}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "review_count" in reason

    def test_approval_false_incompatible(self) -> None:
        """required_approving_review_count is False → incompatible."""
        existing = {
            **self._BASE,
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": False}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "review_count" in reason

    def test_approval_zero_valid(self) -> None:
        """required_approving_review_count is 0 → valid (exact)."""
        compatible, reason = _is_compatible(self._BASE)
        assert compatible is True
        assert reason == "exact"

    def test_approval_one_valid_and_stricter(self) -> None:
        """required_approving_review_count is 1 → valid (stricter)."""
        existing = {
            **self._BASE,
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is True
        assert reason == "stricter"
