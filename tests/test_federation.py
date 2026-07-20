"""Smoke tests for federation scripts."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def _run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_render_federation_descriptor(tmp_path: Path) -> None:
    out = tmp_path / "descriptor.json"
    result = _run_script("render_federation_descriptor.py", "--output", str(out))
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    assert data["kind"] == "agent_federation_descriptor"
    assert data["status"] == "active"
    assert "capabilities" in data
    assert "layer" in data
    assert "endpoints" in data


def test_render_agent_card(tmp_path: Path) -> None:
    out = tmp_path / "agent.json"
    result = _run_script("render_agent_card.py", "--output", str(out))
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    # Name must be a non-empty string — no hardcoded template identity.
    assert isinstance(data["name"], str) and len(data["name"]) > 0
    assert "skills" in data
    assert "federation" in data


def test_export_authority_feed(tmp_path: Path) -> None:
    out_dir = tmp_path / "feed"
    result = _run_script("export_authority_feed.py", "--output-dir", str(out_dir))
    assert result.returncode == 0, result.stderr
    manifest = out_dir / "latest-authority-manifest.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    assert data["kind"] == "source_authority_feed_manifest"


def test_discover_peers_help() -> None:
    result = _run_script("discover_federation_peers.py", "--help")
    assert result.returncode == 0


def test_fetch_peer_authority_help() -> None:
    result = _run_script("fetch_peer_authority.py", "--help")
    assert result.returncode == 0


def test_authority_descriptor_seeds_valid() -> None:
    seeds_path = REPO_ROOT / "data" / "federation" / "authority-descriptor-seeds.json"
    assert seeds_path.exists()
    data = json.loads(seeds_path.read_text())
    assert "descriptor_urls" in data
    assert len(data["descriptor_urls"]) > 0


def test_capabilities_json_valid() -> None:
    caps_path = REPO_ROOT / "docs" / "authority" / "capabilities.json"
    assert caps_path.exists()
    data = json.loads(caps_path.read_text())
    assert data["kind"] == "agent_capability_manifest"
    assert len(data["skills"]) > 0
    assert "federation_interfaces" in data
    assert "produces" in data["federation_interfaces"]


_NADI_SKIP_REASON = "nadi-kit not installed — install with: pip install -e '.[federation]'"


def _load_optional_nadi_kit():
    """Return the ``nadi_kit`` module, or ``None`` if it is not installed.

    Only ``find_spec(...) is None`` (genuine absence) produces ``None``.
    Any ``ImportError`` from a findable but broken module propagates.
    """
    if importlib.util.find_spec("nadi_kit") is None:
        return None
    return importlib.import_module("nadi_kit")


nadi_kit = _load_optional_nadi_kit()


def test_nadi_kit_import() -> None:
    """nadi_kit can be imported and exposes expected API."""
    if nadi_kit is None:
        pytest.skip(_NADI_SKIP_REASON)
    assert hasattr(nadi_kit, "NadiNode")
    assert hasattr(nadi_kit, "NadiMessage")
    assert hasattr(nadi_kit, "NadiTransport")
    assert hasattr(nadi_kit, "NadiHubRelay")


def test_nadi_node_from_peer_json(tmp_path: Path) -> None:
    """NadiNode can be created from a peer.json file."""
    if nadi_kit is None:
        pytest.skip(_NADI_SKIP_REASON)

    peer_data = {
        "identity": {
            "city_id": "test-node",
            "slug": "test-node",
            "repo": "kimeisele/test-node",
            "public_key": "",
        },
        "endpoint": {
            "city_id": "test-node",
            "transport": "filesystem",
            "location": "data/federation",
        },
        "capabilities": ["authority-publishing"],
        "nadi": {
            "outbox": "data/federation/nadi_outbox.json",
            "inbox": "data/federation/nadi_inbox.json",
        },
    }
    peer_json = tmp_path / "peer.json"
    peer_json.write_text(json.dumps(peer_data))

    node = nadi_kit.NadiNode.from_peer_json(peer_json)
    assert node.agent_id == "test-node"
    assert node.repo == "kimeisele/test-node"
    assert node.capabilities == ["authority-publishing"]


def test_nadi_node_emit_and_receive(tmp_path: Path) -> None:
    """NadiNode can emit messages and read them back from transport."""
    if nadi_kit is None:
        pytest.skip(_NADI_SKIP_REASON)

    peer_data = {
        "identity": {"city_id": "emit-test"},
        "capabilities": [],
    }
    peer_json = tmp_path / "peer.json"
    peer_json.write_text(json.dumps(peer_data))

    node = nadi_kit.NadiNode.from_peer_json(peer_json)
    node.emit("ping", {"data": "hello"}, target="steward")

    outbox = node.transport.read_outbox()
    assert len(outbox) == 1
    assert outbox[0].operation == "ping"
    assert outbox[0].target == "steward"
    assert outbox[0].payload["data"] == "hello"


def test_peer_json_exists() -> None:
    """Template ships with a peer.json in data/federation/."""
    peer_path = REPO_ROOT / "data" / "federation" / "peer.json"
    assert peer_path.exists()
    data = json.loads(peer_path.read_text())
    assert "identity" in data
    assert "nadi" in data
    assert "inbox" in data["nadi"]
    assert "outbox" in data["nadi"]


def test_nadi_inbox_exists() -> None:
    """Template ships with a nadi_inbox.json."""
    inbox_path = REPO_ROOT / "data" / "federation" / "nadi_inbox.json"
    assert inbox_path.exists()
    data = json.loads(inbox_path.read_text())
    assert isinstance(data, list)


def test_well_known_descriptor_matches_schema() -> None:
    desc_path = REPO_ROOT / ".well-known" / "agent-federation.json"
    data = json.loads(desc_path.read_text())
    required = {"kind", "version", "repo_id", "display_name", "status", "capabilities", "layer", "endpoints"}
    assert required.issubset(data.keys()), f"Missing fields: {required - data.keys()}"


# ── NADI optional-loader regression tests ────────────────────────────────
#
# These tests run in *every* profile (Core and Federation).  They mock
# ``find_spec`` and ``import_module`` to exercise the guard logic without
# depending on whether the real ``nadi_kit`` is installed.


class TestNadiOptionalLoader:
    """Profile-independent tests for :func:`_load_optional_nadi_kit`."""

    # ── 1. Genuine absence → None ───────────────────────────────────────

    def test_module_truly_absent_returns_none(self, monkeypatch) -> None:
        """``find_spec`` returns ``None`` → ``_load_optional_nadi_kit()``
        returns ``None``."""
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, package=None: None,
        )
        result = _load_optional_nadi_kit()
        assert result is None

    def test_module_absent_produces_skip_in_test(
        self, monkeypatch, tmp_path
    ) -> None:
        """With mocked absence, the import-guarded tests skip."""
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, package=None: None,
        )
        nadi = _load_optional_nadi_kit()
        assert nadi is None
        with pytest.raises(pytest.skip.Exception):
            if nadi is None:
                pytest.skip(_NADI_SKIP_REASON)

    # ── 2. Findable but broken → ImportError propagates ────────────────

    def test_corrupt_module_raises_importerror(self, monkeypatch) -> None:
        """``find_spec`` succeeds but ``import_module`` raises →
        ``_load_optional_nadi_kit()`` propagates the error."""
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, package=None: object(),  # non-None sentinel
        )
        monkeypatch.setattr(
            importlib, "import_module",
            lambda name: (_ for _ in ()).throw(
                ImportError("broken transitive dependency")
            ),
        )
        with pytest.raises(ImportError, match="broken transitive dependency"):
            _load_optional_nadi_kit()

    # ── 3. Module present but API missing → assertion failure ─────────

    def test_missing_api_fails_assertion(self, monkeypatch) -> None:
        """A module without required symbols fails the API check."""
        import types
        fake_module = types.ModuleType("nadi_kit")
        # Deliberately empty — no NadiNode, NadiMessage etc.

        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, package=None: object(),
        )
        monkeypatch.setattr(
            importlib, "import_module",
            lambda name: fake_module,
        )
        mod = _load_optional_nadi_kit()
        assert mod is not None
        with pytest.raises(AssertionError):
            assert hasattr(mod, "NadiNode"), "NadiNode missing from nadi_kit"

    # ── 4. Valid fake module passes API check ─────────────────────────

    def test_valid_fake_module_passes_api_check(self, monkeypatch) -> None:
        """A module with all required symbols passes validation."""
        import types
        fake_module = types.ModuleType("nadi_kit")
        fake_module.NadiNode = object
        fake_module.NadiMessage = object
        fake_module.NadiTransport = object
        fake_module.NadiHubRelay = object

        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, package=None: object(),
        )
        monkeypatch.setattr(
            importlib, "import_module",
            lambda name: fake_module,
        )
        mod = _load_optional_nadi_kit()
        assert mod is not None
        assert hasattr(mod, "NadiNode")
        assert hasattr(mod, "NadiMessage")
        assert hasattr(mod, "NadiTransport")
        assert hasattr(mod, "NadiHubRelay")
