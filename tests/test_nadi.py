"""Gate 3 behaviour tests — NADI path, send, daemon modes, safety.

All remote operations use fakes; no real GitHub mutations.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_NADI_KIT_AVAILABLE = importlib.util.find_spec("nadi_kit") is not None
_NADI_SKIP = pytest.mark.skipif(
    not _NADI_KIT_AVAILABLE,
    reason="nadi-kit not installed — install with: pip install -e '.[federation]'"
)


# ── helpers ────────────────────────────────────────────────────────────────


def _make_peer_json(
    repo_dir: Path,
    *,
    city_id: str = "test-node",
    location: str = "data/federation",
) -> Path:
    peer_dir = repo_dir / "data" / "federation"
    peer_dir.mkdir(parents=True, exist_ok=True)
    peer_data = {
        "identity": {"city_id": city_id, "slug": city_id,
                     "repo": f"test-org/{city_id}", "public_key": ""},
        "endpoint": {"city_id": city_id, "transport": "filesystem",
                     "location": location},
        "capabilities": ["test"],
        "nadi": {
            "outbox": f"{location}/nadi_outbox.json",
            "inbox": f"{location}/nadi_inbox.json",
            "reports": f"{location}/reports/",
            "directives": f"{location}/directives/",
        },
    }
    peer_path = peer_dir / "peer.json"
    peer_path.write_text(json.dumps(peer_data, indent=2) + "\n")
    return peer_path


def _make_scripts(repo_dir: Path, *names: str) -> Path:
    scripts_dest = repo_dir / "scripts"
    scripts_dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = _SCRIPTS / name
        if src.exists():
            (scripts_dest / name).write_text(src.read_text())
    return scripts_dest


def _file_tree_snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and ".git" not in f.parts \
           and "__pycache__" not in f.parts:
            snap[str(f.relative_to(root))] = hashlib.sha256(
                f.read_bytes()).hexdigest()
    return snap


# ── Tests ──────────────────────────────────────────────────────────────────


@_NADI_SKIP
class TestNadiSendViaNadiNode:
    """nadi_send must emit through NadiNode, producing real NadiMessages."""

    def test_send_produces_signed_message(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        _make_scripts(tmp_path, "nadi_send.py", "federation_utils.py")
        result = subprocess.run(
            [sys.executable, str(tmp_path / "scripts" / "nadi_send.py"),
             "send", "--to", "proof-target", "--op", "proof-op",
             "--payload", '{"value": 1}', "--priority", "3",
             "--ttl-seconds", "60"],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 0, (
            f"exit={result.returncode}\nstderr={result.stderr}")
        outbox = json.loads(
            (tmp_path / "data" / "federation" / "nadi_outbox.json").read_text())
        assert len(outbox) == 1
        msg = outbox[0]
        assert msg["operation"] == "proof-op"
        assert msg["target"] == "proof-target"
        assert msg["payload"] == {"value": 1}
        assert msg["priority"] == 3
        assert msg.get("signature"), "must be signed"
        assert msg.get("payload_hash"), "must have payload_hash"
        assert isinstance(msg["source"], str) and len(msg["source"]) > 0
        for legacy in ("source_city_id", "target_city_id", "ttl_ms",
                       "envelope_id", "nadi_type", "nadi_op"):
            assert legacy not in msg

    def test_send_from_outside_repo_dir(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        _make_scripts(tmp_path, "nadi_send.py", "federation_utils.py")
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run(
                [sys.executable, str(tmp_path / "scripts" / "nadi_send.py"),
                 "send", "--to", "ext-test", "--op", "ping"],
                capture_output=True, text=True, cwd=td,
            )
        assert result.returncode == 0, f"send failed: {result.stderr}"
        assert (tmp_path / "data" / "federation" / "nadi_outbox.json").exists()
        assert not Path(td).joinpath("nadi_outbox.json").exists()

    def test_corrupt_peer_json_errors(self, tmp_path: Path) -> None:
        _make_scripts(tmp_path, "nadi_send.py", "federation_utils.py")
        peer_dir = tmp_path / "data" / "federation"
        peer_dir.mkdir(parents=True, exist_ok=True)
        (peer_dir / "peer.json").write_text("{not json!!!")
        result = subprocess.run(
            [sys.executable, str(tmp_path / "scripts" / "nadi_send.py"),
             "send", "--to", "test", "--op", "test"],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode != 0

    def test_payload_non_dict_rejected(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        _make_scripts(tmp_path, "nadi_send.py", "federation_utils.py")
        result = subprocess.run(
            [sys.executable, str(tmp_path / "scripts" / "nadi_send.py"),
             "send", "--to", "test", "--op", "test",
             "--payload", '["not", "an", "object"]'],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode != 0
        outbox_path = tmp_path / "data" / "federation" / "nadi_outbox.json"
        if outbox_path.exists():
            assert len(json.loads(outbox_path.read_text())) == 0


class TestNadiPathContract:
    """Centralized NadiPathContract in federation_utils."""

    def test_correct_paths_return_contract(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        from federation_utils import resolve_and_validate_nadi_paths
        c = resolve_and_validate_nadi_paths(
            tmp_path / "data" / "federation" / "peer.json")
        assert c.federation_dir == (tmp_path / "data" / "federation").resolve()
        assert c.outbox_path == (tmp_path / "data" / "federation" / "nadi_outbox.json").resolve()
        assert c.inbox_path == (tmp_path / "data" / "federation" / "nadi_inbox.json").resolve()
        assert c.peer_path == (tmp_path / "data" / "federation" / "peer.json").resolve()

    def test_wrong_outbox_raises(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        peer_path = tmp_path / "data" / "federation" / "peer.json"
        peer = json.loads(peer_path.read_text())
        peer["nadi"]["outbox"] = "wrong/outbox.json"
        peer_path.write_text(json.dumps(peer, indent=2))
        from federation_utils import NadiPathError, resolve_and_validate_nadi_paths
        with pytest.raises(NadiPathError, match="outbox"):
            resolve_and_validate_nadi_paths(peer_path)

    def test_wrong_inbox_raises(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        peer_path = tmp_path / "data" / "federation" / "peer.json"
        peer = json.loads(peer_path.read_text())
        peer["nadi"]["inbox"] = "wrong/inbox.json"
        peer_path.write_text(json.dumps(peer, indent=2))
        from federation_utils import NadiPathError, resolve_and_validate_nadi_paths
        with pytest.raises(NadiPathError, match="inbox"):
            resolve_and_validate_nadi_paths(peer_path)

    def test_validation_is_read_only(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        snap_before = _file_tree_snapshot(tmp_path)
        from federation_utils import resolve_and_validate_nadi_paths
        resolve_and_validate_nadi_paths(
            tmp_path / "data" / "federation" / "peer.json")
        snap_after = _file_tree_snapshot(tmp_path)
        assert snap_before == snap_after

    def test_send_and_daemon_share_same_contract(self, tmp_path: Path) -> None:
        """Both send and daemon resolve identical paths from same peer."""
        _make_peer_json(tmp_path)
        peer_path = tmp_path / "data" / "federation" / "peer.json"

        from federation_utils import resolve_and_validate_nadi_paths
        c = resolve_and_validate_nadi_paths(peer_path)

        # Both loaders should produce the same contract values
        from nadi_daemon import resolve_and_validate_nadi_paths as daemon_resolve
        c2 = daemon_resolve(peer_path)
        assert c.federation_dir == c2.federation_dir
        assert c.outbox_path == c2.outbox_path
        assert c.inbox_path == c2.inbox_path

    def test_mismatch_rejected_identically(self, tmp_path: Path) -> None:
        """Both send and daemon reject the same bad path."""
        _make_peer_json(tmp_path)
        peer_path = tmp_path / "data" / "federation" / "peer.json"
        peer = json.loads(peer_path.read_text())
        peer["nadi"]["outbox"] = "bad/path.json"
        peer_path.write_text(json.dumps(peer, indent=2))

        from federation_utils import NadiPathError
        from nadi_daemon import resolve_and_validate_nadi_paths as daemon_resolve
        with pytest.raises(NadiPathError, match="outbox"):
            daemon_resolve(peer_path)

import pytest  # noqa: E402


@_NADI_SKIP
class TestDaemonReadOnlyLocal:
    """--once must be strictly read-only: no keys, no files, no mutations."""

    def test_once_creates_no_files(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        _make_scripts(tmp_path, "nadi_daemon.py", "federation_utils.py")
        keys_path = tmp_path / "data" / "federation" / ".node_keys.json"
        assert not keys_path.exists()
        snap_before = _file_tree_snapshot(tmp_path)
        result = subprocess.run(
            [sys.executable, str(tmp_path / "scripts" / "nadi_daemon.py"),
             "--once"],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"daemon failed: {result.stderr}"
        assert "Node:" in result.stdout
        snap_after = _file_tree_snapshot(tmp_path)
        assert snap_before == snap_after, (
            f"added: {set(snap_after) - set(snap_before)}")
        assert not keys_path.exists()

    def test_once_no_gh_subprocess(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        _make_scripts(tmp_path, "nadi_daemon.py", "federation_utils.py")
        result = subprocess.run(
            [sys.executable, str(tmp_path / "scripts" / "nadi_daemon.py"),
             "--once"],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert "gh api" not in result.stdout.lower()


class TestDaemonFakeRelay:
    """Relay modes with a fully controlled fake node."""

    @staticmethod
    def _fake_node():
        node = MagicMock()
        node.agent_id = "fake-agent"
        node.heartbeat = MagicMock(return_value=[])
        node.sync = MagicMock(return_value={
            "pulled": 0, "processed": 0, "pushed": 0, "expired": 0})
        return node

    def test_relay_banner_appears(self) -> None:
        from nadi_daemon import _execute_mode
        args = argparse.Namespace(
            once=True, relay=True, interval=900, health=1.0, head_agent=None)
        fake = self._fake_node()
        import io
        saved = sys.stdout
        try:
            sys.stdout = io.StringIO()
            _execute_mode(args, node_loader=lambda: (fake, 0))
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = saved
        assert "REMOTE RELAY ENABLED" in output

    def test_relay_once_calls_heartbeat_and_sync(self) -> None:
        from nadi_daemon import _execute_mode
        args = argparse.Namespace(
            once=True, relay=True, interval=900, health=1.0, head_agent=None)
        fake = self._fake_node()
        _execute_mode(args, node_loader=lambda: (fake, 0))
        assert fake.heartbeat.call_count == 1
        assert fake.sync.call_count == 1

    def test_local_once_does_not_call_node_loader(self) -> None:
        from nadi_daemon import _execute_mode
        calls = [0]

        def _counting():
            calls[0] += 1
            return MagicMock(), 0

        args = argparse.Namespace(
            once=True, relay=False, interval=900, health=1.0, head_agent=None)
        _execute_mode(args, node_loader=_counting)
        assert calls[0] == 0


class TestMissingNadiKit:
    """When nadi-kit is absent, tools give clean UX, not traceback."""

    def test_send_guard_absent(self) -> None:
        from nadi_send import _load_nadi_node
        with patch("importlib.util.find_spec", return_value=None):
            node, exit_code = _load_nadi_node()
            assert node is None
            assert exit_code == 1

    def test_daemon_guard_absent(self) -> None:
        from nadi_daemon import _load_nadi_node
        with patch("importlib.util.find_spec", return_value=None):
            node, exit_code = _load_nadi_node()
            assert node is None
            assert exit_code == 1

    def test_broken_module_not_masked(self, monkeypatch) -> None:
        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, package=None: object())
        import builtins
        import sys as _sys
        _real_import = builtins.__import__
        _sys.modules.pop("nadi_kit", None)
        def _fail(name, *a, **kw):
            if name == "nadi_kit":
                raise ImportError("broken deps")
            return _real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", _fail)
        from nadi_daemon import _load_nadi_node
        node, exit_code = _load_nadi_node()
        assert node is None
        assert exit_code == 1

    def test_once_without_nadi_kit_clean_failure(self, tmp_path: Path) -> None:
        """CLI --once without nadi-kit: clean error, no traceback, no mutation."""
        _make_peer_json(tmp_path)
        _make_scripts(tmp_path, "nadi_daemon.py", "federation_utils.py")
        snap_before = _file_tree_snapshot(tmp_path)
        # With nadi-kit installed, --once succeeds.
        # We test absence via direct main() call with mock.
        from nadi_daemon import main as daemon_main
        with patch("importlib.util.find_spec", return_value=None):
            exit_code = daemon_main(["--once"])
            assert exit_code == 1
        snap_after = _file_tree_snapshot(tmp_path)
        assert snap_before == snap_after

    def test_once_with_broken_nadi_kit_fails_visibly(self, tmp_path,
                                                      monkeypatch) -> None:
        """findable but broken nadi-kit → visible failure, no mutation."""
        _make_peer_json(tmp_path)
        _make_scripts(tmp_path, "nadi_daemon.py", "federation_utils.py")
        snap_before = _file_tree_snapshot(tmp_path)

        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, package=None: object())
        import builtins
        import sys as _sys
        _real_import = builtins.__import__
        _sys.modules.pop("nadi_kit", None)
        def _fail(name, *a, **kw):
            if name == "nadi_kit":
                raise ImportError("broken deps")
            return _real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", _fail)

        from nadi_daemon import main as daemon_main
        exit_code = daemon_main(["--once"])
        assert exit_code == 1, f"broken module must fail, got {exit_code}"
        snap_after = _file_tree_snapshot(tmp_path)
        assert snap_before == snap_after


class TestSetupPeerPreservation:
    """setup_node must preserve existing NADI data."""

    def _run_setup(self, repo_dir: Path, **kwargs):
        _make_scripts(repo_dir,
                      "setup_node.py", "federation_utils.py",
                      "render_federation_descriptor.py", "render_agent_card.py",
                      "export_authority_feed.py", "discover_federation_peers.py")
        gov_src = _SCRIPTS / "governance"
        gov_dest = repo_dir / "scripts" / "governance"
        if gov_src.exists():
            gov_dest.mkdir(exist_ok=True)
            for gov_file in gov_src.iterdir():
                if gov_file.is_file() and gov_file.suffix == ".py":
                    (gov_dest / gov_file.name).write_text(gov_file.read_text())
        (repo_dir / "docs" / "authority").mkdir(parents=True, exist_ok=True)
        caps_src = _SCRIPTS.parent / "docs" / "authority" / "capabilities.json"
        if caps_src.exists():
            (repo_dir / "docs" / "authority" / "capabilities.json").write_text(
                caps_src.read_text())
        seeds_dest = (repo_dir / "data" / "federation"
                      / "authority-descriptor-seeds.json")
        seeds_dest.parent.mkdir(parents=True, exist_ok=True)
        seeds_src = _SCRIPTS.parent / "data" / "federation" / "authority-descriptor-seeds.json"
        if seeds_src.exists():
            seeds_dest.write_text(seeds_src.read_text())
        else:
            seeds_dest.write_text(json.dumps({"descriptor_urls": []}))
        args = [sys.executable, str(repo_dir / "scripts" / "setup_node.py"),
                "--non-interactive"]
        for k, v in kwargs.items():
            args.append(f"--{k.replace('_', '-')}")
            args.append(str(v))
        return subprocess.run(
            args, capture_output=True, text=True, cwd=str(repo_dir),
            env={"PATH": os.environ.get("PATH", ""),
                 "HOME": os.environ.get("HOME", ""),
                 "USER": os.environ.get("USER", ""),
                 "TMPDIR": os.environ.get("TMPDIR", "/tmp")},
        )

    def test_existing_outbox_preserved(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        outbox_path = tmp_path / "data" / "federation" / "nadi_outbox.json"
        outbox_path.write_text(
            json.dumps([{"id": "existing-msg", "operation": "keep-me"}]))
        result = self._run_setup(
            tmp_path, name="Test", role="relay", repo="test-org/test-node")
        assert result.returncode == 0
        assert json.loads(outbox_path.read_text())[0]["id"] == "existing-msg"

    def test_corrupt_outbox_not_overwritten(self, tmp_path: Path) -> None:
        _make_peer_json(tmp_path)
        outbox_path = tmp_path / "data" / "federation" / "nadi_outbox.json"
        corrupt = "!!! not json !!!"
        outbox_path.write_text(corrupt)
        self._run_setup(tmp_path, name="Test", role="relay",
                        repo="test-org/test-node")
        assert outbox_path.read_text() == corrupt

    def test_peer_public_key_preserved(self, tmp_path: Path) -> None:
        peer_path = _make_peer_json(tmp_path)
        peer = json.loads(peer_path.read_text())
        peer["identity"]["public_key"] = "my-key"
        peer_path.write_text(json.dumps(peer, indent=2))
        self._run_setup(tmp_path, name="Test", role="governance",
                        repo="test-org/test-node")
        updated = json.loads(peer_path.read_text())
        assert updated["identity"]["public_key"] == "my-key"
        assert "governance-participation" in updated["capabilities"]
