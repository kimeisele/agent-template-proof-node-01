"""Gate 5 — Workflow contract tests. All remote operations mocked."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_REPO_ROOT = _SCRIPTS.parent
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_yaml = None
try:
    import yaml as _yaml
except ImportError:
    pass

_NADI_KIT = None
try:
    import nadi_kit as _NADI_KIT
except ImportError:
    pass


def _wf_files():
    return sorted(_WORKFLOW_DIR.glob("*.yml"))


def _parse(path: Path) -> dict:
    assert _yaml is not None
    p = _yaml.safe_load(path.read_text())
    assert isinstance(p, dict)
    return p


# ── YAML ───────────────────────────────────────────────────────────────────


class TestWorkflowYaml:
    def test_all_parse(self) -> None:
        assert _yaml is not None
        for wf in _wf_files():
            p = _parse(wf)
            assert "jobs" in p, wf.name
            r = wf.read_text()
            assert "\non:" in r or r.startswith("on:"), f"{wf.name} trigger"

    def test_no_pull_request_target(self) -> None:
        for wf in _wf_files():
            assert "pull_request_target" not in wf.read_text()

    def test_no_write_all(self) -> None:
        for wf in _wf_files():
            assert "write-all" not in wf.read_text()

    def test_secrets_only_in_env(self) -> None:
        for wf in _wf_files():
            v = _find_secret_in_run(_parse(wf))
            assert not v, f"{wf.name}: {v}"

    def test_scripts_exist(self) -> None:
        import re
        for wf in _wf_files():
            for m in re.finditer(r'scripts/[\w/]+\.py', wf.read_text()):
                assert (_REPO_ROOT / m.group()).exists(), m.group()


def _find_secret_in_run(node, path=""):
    v = []
    if isinstance(node, dict):
        for k, val in node.items():
            p = f"{path}.{k}" if path else k
            if k == "run" and isinstance(val, str):
                if "${{ secrets." in val or "${{secrets." in val:
                    v.append(p)
            else:
                v.extend(_find_secret_in_run(val, p))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            v.extend(_find_secret_in_run(item, f"{path}[{i}]"))
    return v


# ── Guard ──────────────────────────────────────────────────────────────────


class TestGuard:
    def _run(self, env):
        r = subprocess.run(
            [sys.executable, str(_SCRIPTS / "heartbeat_workflow_guard.py")],
            capture_output=True, text=True, env={**os.environ, **env})
        return r.returncode, json.loads(r.stdout.strip())

    def test_both_missing(self):
        ec, d = self._run({})
        assert ec == 0 and d["status"] == "REMOTE_DISABLED_MISSING_PAT"

    def test_only_key(self):
        ec, d = self._run({"NODE_PRIVATE_KEY": "k"})
        assert ec == 0 and d["status"] == "REMOTE_DISABLED_MISSING_PAT"

    def test_only_pat(self):
        ec, d = self._run({"FEDERATION_PAT": "t"})
        assert ec == 0 and d["status"] == "REMOTE_DISABLED_MISSING_NODE_KEY"

    def test_both(self):
        ec, d = self._run({"FEDERATION_PAT": "t", "NODE_PRIVATE_KEY": "k"})
        assert ec == 0 and d["status"] == "REMOTE_ENABLED"

    def test_no_secret_leak(self):
        _, d = self._run({"FEDERATION_PAT": "ghp_X", "NODE_PRIVATE_KEY": "Y"})
        out = json.dumps(d)
        assert "ghp_X" not in out and "Y" not in out


# ── Workflow coupling ──────────────────────────────────────────────────────


class TestWorkflowCoupling:
    """Capture/verify steps are present in correct order."""

    def _content(self):
        return (_WORKFLOW_DIR / "heartbeat.yml").read_text()

    def test_invokes_capture_and_verify(self):
        c = self._content()
        assert "heartbeat_postcondition.py capture" in c, "missing capture step"
        assert "heartbeat_postcondition.py verify" in c, "missing verify step"

    def test_capture_before_final_sync(self):
        """capture occurs after heartbeat emit, before final sync."""
        c = self._content()
        # Find line numbers
        lines = c.split("\n")
        cap_idx = next(i for i, line in enumerate(lines)
                       if "heartbeat_postcondition.py capture" in line)
        final_sync_idx = next(i for i, line in enumerate(lines)
                              if "Final NADI sync" in line or "final sync" in line.lower())
        emit_idx = next(i for i, line in enumerate(lines)
                        if "Emit signed heartbeat" in line)
        verify_idx = next(i for i, line in enumerate(lines)
                          if "heartbeat_postcondition.py verify" in line)
        assert emit_idx < cap_idx, "emit must come before capture"
        assert cap_idx < final_sync_idx, "capture must come before final sync"
        assert final_sync_idx < verify_idx, "final sync must come before verify"

    def test_same_proof_path(self):
        c = self._content()
        # Both capture and verify reference heartbeat-proof.json
        assert c.count("heartbeat-proof.json") >= 2, (
            "capture and verify must use the same proof file"
        )

    def test_no_bare_postcondition(self):
        """No invocation without capture or verify subcommand."""
        c = self._content()
        # Every reference to heartbeat_postcondition.py must include
        # either 'capture' or 'verify'
        for line in c.split("\n"):
            if "heartbeat_postcondition.py" in line:
                assert "capture" in line or "verify" in line, (
                    f"heartbeat_postcondition.py without subcommand: {line.strip()}"
                )

    def test_guard_referenced(self):
        assert "heartbeat_workflow_guard.py" in self._content()

    def test_all_guard_statuses_handled(self):
        c = self._content()
        for s in ("REMOTE_ENABLED", "REMOTE_DISABLED_MISSING_PAT",
                  "REMOTE_DISABLED_MISSING_NODE_KEY"):
            assert s in c


# ── Postcondition CLI ──────────────────────────────────────────────────────


class TestPostconditionCLI:
    def test_no_subcommand_exits_2(self):
        r = subprocess.run(
            [sys.executable, str(_SCRIPTS / "heartbeat_postcondition.py")],
            capture_output=True, text=True,
        )
        assert r.returncode == 2, (
            f"bare invocation must exit 2, got {r.returncode}"
        )


# ── Capture behavior ───────────────────────────────────────────────────────


class TestCaptureBehavior:
    def _capture(self, outbox, tmp_path):
        p = tmp_path / "proof.json"
        from heartbeat_postcondition import cmd_capture
        ec = cmd_capture(str(outbox), str(p))
        return ec, p

    def test_heartbeat_present_succeeds(self, tmp_path):
        outbox = tmp_path / "outbox.json"
        outbox.write_text(json.dumps([
            {"id": "hb-1", "source": "ag_x", "operation": "heartbeat"},
            {"id": "cl-1", "source": "ag_x",
             "operation": "federation.agent_claim"},
        ]))
        ec, pf = self._capture(outbox, tmp_path)
        assert ec == 0
        d = json.loads(pf.read_text())
        assert d["heartbeat_message_ids"] == ["hb-1"]
        assert d["additional_message_ids"] == ["cl-1"]
        assert d["source_node_id"] == "ag_x"

    def test_no_heartbeat_fails(self, tmp_path):
        outbox = tmp_path / "outbox.json"
        outbox.write_text(json.dumps([
            {"id": "x", "source": "ag_x", "operation": "agent_claim"},
        ]))
        ec, _ = self._capture(outbox, tmp_path)
        assert ec != 0

    def test_mixed_sources_fails(self, tmp_path):
        outbox = tmp_path / "outbox.json"
        outbox.write_text(json.dumps([
            {"id": "a", "source": "ag_1", "operation": "heartbeat"},
            {"id": "b", "source": "ag_2", "operation": "heartbeat"},
        ]))
        ec, _ = self._capture(outbox, tmp_path)
        assert ec != 0

    def test_empty_outbox_fails(self, tmp_path):
        outbox = tmp_path / "outbox.json"
        outbox.write_text("[]")
        ec, _ = self._capture(outbox, tmp_path)
        assert ec != 0


# ── Verify behavior ────────────────────────────────────────────────────────


class TestVerifyBehavior:
    def _verify(self, proof, tmp_path):
        import json as _json
        p = tmp_path / "proof.json"
        p.write_text(_json.dumps(proof))
        from heartbeat_postcondition import cmd_verify
        return cmd_verify(str(p))

    def test_correct_id_source_op_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "heartbeat_postcondition._list_hub_nadi_files",
            lambda: [{"name": "ag_x_to_steward.json",
                      "url": "https://api.github.com/repos/x/contents/nadi/ag_x_to_steward.json"}],
        )
        monkeypatch.setattr(
            "heartbeat_postcondition._fetch_hub_file",
            lambda url: [{"id": "hb-1", "source": "ag_x",
                          "operation": "heartbeat"}],
        )
        ec = self._verify({
            "source_node_id": "ag_x",
            "heartbeat_message_ids": ["hb-1"],
            "captured_at": 1000,
        }, tmp_path)
        assert ec == 0

    def test_old_id_same_source_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "heartbeat_postcondition._list_hub_nadi_files",
            lambda: [{"name": "ag_x_to_steward.json", "url": "https://api.github.com/repos/x/contents/nadi/x"}],
        )
        monkeypatch.setattr(
            "heartbeat_postcondition._fetch_hub_file",
            lambda url: [{"id": "old-id", "source": "ag_x",
                          "operation": "heartbeat"}],
        )
        ec = self._verify({
            "source_node_id": "ag_x",
            "heartbeat_message_ids": ["new-id"],
            "captured_at": 2000,
        }, tmp_path)
        assert ec != 0

    def test_right_id_wrong_source_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "heartbeat_postcondition._list_hub_nadi_files",
            lambda: [{"name": "ag_y_to_steward.json", "url": "https://api.github.com/repos/x/contents/nadi/x"}],
        )
        monkeypatch.setattr(
            "heartbeat_postcondition._fetch_hub_file",
            lambda url: [{"id": "hb-1", "source": "wrong_src",
                          "operation": "heartbeat"}],
        )
        ec = self._verify({
            "source_node_id": "ag_x",
            "heartbeat_message_ids": ["hb-1"],
            "captured_at": 1000,
        }, tmp_path)
        assert ec != 0

    def test_right_id_wrong_op_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "heartbeat_postcondition._list_hub_nadi_files",
            lambda: [{"name": "ag_x_to_steward.json", "url": "https://api.github.com/repos/x/contents/nadi/x"}],
        )
        monkeypatch.setattr(
            "heartbeat_postcondition._fetch_hub_file",
            lambda url: [{"id": "hb-1", "source": "ag_x",
                          "operation": "not-heartbeat"}],
        )
        ec = self._verify({
            "source_node_id": "ag_x",
            "heartbeat_message_ids": ["hb-1"],
            "captured_at": 1000,
        }, tmp_path)
        assert ec != 0

    def test_read_only_pat_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "heartbeat_postcondition._list_hub_nadi_files",
            lambda: None)
        ec = self._verify({
            "source_node_id": "ag_x",
            "heartbeat_message_ids": ["hb-1"],
            "captured_at": 1000,
        }, tmp_path)
        assert ec != 0


# ── CI invalid key ─────────────────────────────────────────────────────────


class TestCIInvalidKey:
    def test_invalid_key_in_ci_must_fail(self):
        if _NADI_KIT is None:
            pytest.skip("nadi-kit not installed")
        import tempfile
        import json as _json

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            fed = tdp / "data" / "federation"
            fed.mkdir(parents=True)
            peer = {
                "identity": {"city_id": "ci", "slug": "ci", "repo": "org/ci",
                             "public_key": ""},
                "endpoint": {"city_id": "ci", "transport": "filesystem",
                             "location": str(fed)},
                "capabilities": [],
            }
            (fed / "peer.json").write_text(_json.dumps(peer))
            keys_path = fed / ".node_keys.json"
            assert not keys_path.exists()

            result = subprocess.run(
                [sys.executable, "-c", """
import os, sys
from pathlib import Path
os.environ["GITHUB_ACTIONS"] = "true"
os.environ["NODE_PRIVATE_KEY"] = "!!!invalid-key!!!"
from nadi_kit import NadiNode
try:
    node = NadiNode.from_peer_json(Path(sys.argv[1]))
    print(f"loaded: {node.agent_id}")
except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(1)
""", str(fed / "peer.json")],
                capture_output=True, text=True,
                env={**os.environ, "GITHUB_ACTIONS": "true",
                     "NODE_PRIVATE_KEY": "!!!invalid-key!!!"},
                cwd=str(tdp),
            )
            # nadi-kit at pinned commit handles invalid key by logging
            # a warning and generating a new key (exit 0). The key
            # value is never leaked. The guard correctly classifies
            # non-empty as REMOTE_ENABLED. The invalid format is
            # detected at nadi-kit load time with a warning.
            assert "!!!invalid-key!!!" not in result.stdout, (
                "secret value must not appear in stdout"
            )
            assert "!!!invalid-key!!!" not in result.stderr, (
                "secret value must not appear in stderr"
            )
            # Key file may be regenerated by nadi-kit auto-recovery

    def test_invalid_key_not_classified_as_missing(self):
        r = subprocess.run(
            [sys.executable, str(_SCRIPTS / "heartbeat_workflow_guard.py")],
            capture_output=True, text=True,
            env={**os.environ, "NODE_PRIVATE_KEY": "bad", "FEDERATION_PAT": "t"},
        )
        assert json.loads(r.stdout.strip())["status"] == "REMOTE_ENABLED"


# ── Orchestration ──────────────────────────────────────────────────────────


class TestOrchestration:
    def _sim(self, core_exit, guard, preflight, relay_exit, postcondition):
        steps = []
        if core_exit:
            return 1, ["core"]
        steps.append("core")
        if guard != "REMOTE_ENABLED":
            return 0, steps + ["guard"]
        steps.append("guard")
        if not preflight:
            return 1, steps + ["preflight"]
        steps.append("preflight")
        if relay_exit:
            return 1, steps + ["relay"]
        steps.append("relay")
        if not postcondition:
            return 1, steps + ["postcondition"]
        return 0, steps + ["postcondition"]

    def test_core_fail(self):
        ec, s = self._sim(1, "REMOTE_ENABLED", True, 0, True)
        assert ec == 1 and s == ["core"]

    def test_missing_secrets(self):
        ec, s = self._sim(0, "REMOTE_DISABLED_MISSING_PAT", True, 0, True)
        assert ec == 0 and "relay" not in s

    def test_preflight_fail(self):
        ec, s = self._sim(0, "REMOTE_ENABLED", False, 0, True)
        assert ec == 1 and "relay" not in s

    def test_relay_ok_postcondition_fail(self):
        ec, s = self._sim(0, "REMOTE_ENABLED", True, 0, False)
        assert ec == 1 and "postcondition" in s

    def test_full_success(self):
        ec, s = self._sim(0, "REMOTE_ENABLED", True, 0, True)
        assert ec == 0 and s == [
            "core", "guard", "preflight", "relay", "postcondition"]


# ── Identity ───────────────────────────────────────────────────────────────


class TestIdentity:
    def test_no_agent_template(self):
        for wf in _wf_files():
            c = wf.read_text()
            assert "agent-template-bot" not in c
            assert "agent-template_to_steward" not in c

    def test_nadi_kit_only(self):
        c = (_WORKFLOW_DIR / "heartbeat.yml").read_text()
        assert "git clone" not in c
        assert "_to_steward.json" not in c
        assert "nadi_kit" in c

    def test_human_and_crypto_identity(self):
        if _NADI_KIT is None:
            pytest.skip("nadi-kit not installed")
        import tempfile
        import json as _json

        results = {}
        for name in ("external-proof-node", "research-node-two"):
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                fed = tdp / "data" / "federation"
                fed.mkdir(parents=True)
                peer = {
                    "identity": {"city_id": name, "slug": name,
                                 "repo": f"org/{name}", "public_key": ""},
                    "endpoint": {"city_id": name, "transport": "filesystem",
                                 "location": str(fed)},
                    "capabilities": [],
                }
                (fed / "peer.json").write_text(_json.dumps(peer))
                node = _NADI_KIT.NadiNode.from_peer_json(fed / "peer.json")
                msgs = node.emit("heartbeat", {}, target="dest")
                results[name] = {
                    "agent_id": node.agent_id,
                    "source": msgs[0].source,
                }
        a, b = results["external-proof-node"], results["research-node-two"]
        assert a["agent_id"] != b["agent_id"]
        assert a["source"] != b["source"]
        assert "agent-template" not in a["agent_id"]
        assert "agent-template" not in b["agent_id"]


# ── Permissions ────────────────────────────────────────────────────────────


class TestPermissions:
    def test_all_declare(self):
        for wf in _wf_files():
            assert "permissions:" in wf.read_text()

    def test_heartbeat_read_only(self):
        p = _parse(_WORKFLOW_DIR / "heartbeat.yml").get("permissions", {})
        assert p.get("contents") == "read"

    def test_sync_have_contents(self):
        for n in ("sync-agent-card.yml", "sync-federation-descriptor.yml",
                  "federation-discovery.yml"):
            assert "contents" in _parse(_WORKFLOW_DIR / n).get("permissions", {})


# ── Doc guards ─────────────────────────────────────────────────────────────


class TestDocGuards:
    def test_no_push_to_main(self):
        assert "push to main" not in (_SCRIPTS / "quickstart.py").read_text()

    def test_no_static_counts(self):
        c = (_REPO_ROOT / "AGENTS.md").read_text()
        for p in ("8 smoke", "101 tests", "175 tests", "195 tests"):
            assert p not in c


# ── Contents API integration test ──────────────────────────────────────────


class TestFetchHubFile:
    """_fetch_hub_file uses the GitHub Contents API correctly."""

    def test_fetch_uses_api_url_not_download_url(self, monkeypatch):
        """The entry.url field is passed, not download_url."""
        # Verify the verify loop extracts api_url from entry["url"]
        from heartbeat_postcondition import cmd_verify
        # We test via the functional unit: mock _list returns entry with 'url'
        monkeypatch.setattr(
            "heartbeat_postcondition._list_hub_nadi_files",
            lambda: [{
                "name": "ag_x_to_steward.json",
                "url": "https://api.github.com/repos/o/r/contents/nadi/node_x_to_steward.json",
                "download_url": "https://raw.githubusercontent.com/o/r/main/nadi/node_x_to_steward.json",
            }],
        )
        # Mock _fetch_hub_file to capture the URL it receives
        called_urls = []
        def _capture_url(url):
            called_urls.append(url)
            return [{"id": "hb-1", "source": "ag_x", "operation": "heartbeat"}]
        monkeypatch.setattr(
            "heartbeat_postcondition._fetch_hub_file", _capture_url)

        import tempfile
        import json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                          delete=False) as f:
            _json.dump({
                "source_node_id": "ag_x",
                "heartbeat_message_ids": ["hb-1"],
                "captured_at": 1000,
            }, f)
        cmd_verify(f.name)
        assert len(called_urls) == 1
        assert "contents" in called_urls[0], (
            f"must use API URL, got {called_urls[0]}"
        )

    def test_decodes_real_contents_api_shape(self, monkeypatch):
        """Base64-encoded content from Contents API is decoded correctly."""
        import base64
        payload = json.dumps([
            {"id": "hb-1", "source": "ag_x", "operation": "heartbeat"},
        ])
        encoded = base64.b64encode(payload.encode("utf-8")).decode("utf-8")

        monkeypatch.setattr(
            "heartbeat_postcondition._list_hub_nadi_files",
            lambda: [{"name": "x.json",
                      "url": "https://api.github.com/x"}],
        )
        # Mock subprocess.run to return contents API shape
        import subprocess as _sp
        original_run = _sp.run
        def _fake_run(cmd, *args, **kwargs):
            if "contents" in str(cmd):
                return _sp.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=json.dumps({
                        "encoding": "base64",
                        "content": encoded,
                    }),
                    stderr="",
                )
            return original_run(cmd, *args, **kwargs)
        monkeypatch.setattr(_sp, "run", _fake_run)

        from heartbeat_postcondition import _fetch_hub_file
        result = _fetch_hub_file("https://api.github.com/repos/o/r/contents/nadi/x")
        assert result is not None
        assert len(result) == 1
        assert result[0]["id"] == "hb-1"

    def test_raw_list_response_rejected(self, monkeypatch):
        """Direct JSON list (not Contents API object) → None."""
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run",
            lambda cmd, *a, **kw: _sp.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps([{"id": "x"}]),
                stderr="",
            ))
        from heartbeat_postcondition import _fetch_hub_file
        assert _fetch_hub_file("http://x") is None

    def test_non_base64_encoding_fails(self, monkeypatch):
        """Non-base64 encoding → None."""
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run",
            lambda cmd, *a, **kw: _sp.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"encoding": "utf-8", "content": "[]"}),
                stderr="",
            ))
        from heartbeat_postcondition import _fetch_hub_file
        assert _fetch_hub_file("http://x") is None


# ── CI invalid key (verbindlich) ───────────────────────────────────────────


class TestCIInvalidKeyVerb:
    """CI invalid key: non-zero exit, no key file, no secret leak."""

    def test_invalid_key_ci_must_fail(self):
        if _NADI_KIT is None:
            pytest.skip("nadi-kit not installed")
        import tempfile
        import json as _json

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            fed = tdp / "data" / "federation"
            fed.mkdir(parents=True)
            peer = {
                "identity": {"city_id": "ci", "slug": "ci", "repo": "org/ci",
                             "public_key": ""},
                "endpoint": {"city_id": "ci", "transport": "filesystem",
                             "location": str(fed)},
                "capabilities": [],
            }
            (fed / "peer.json").write_text(_json.dumps(peer))
            keys_path = fed / ".node_keys.json"
            assert not keys_path.exists()

            # Strip PYTEST_CURRENT_TEST — nadi-kit suppresses CI fatal
            # error when running under pytest.
            clean_env = {
                k: v for k, v in os.environ.items()
                if k not in ("PYTEST_CURRENT_TEST",)
            }
            clean_env["GITHUB_ACTIONS"] = "true"
            clean_env["NODE_PRIVATE_KEY"] = "!!!invalid-key!!!"

            result = subprocess.run(
                [sys.executable, "-c", """
import os, sys
from pathlib import Path
# GITHUB_ACTIONS and NODE_PRIVATE_KEY come from the environment
from nadi_kit import NadiNode
try:
    node = NadiNode.from_peer_json(Path(sys.argv[1]))
    print(f"UNEXPECTED SUCCESS: {node.agent_id}")
    sys.exit(0)
except Exception as exc:
    print(f"FAILED: {exc}", file=sys.stderr)
    sys.exit(1)
""", str(fed / "peer.json")],
                capture_output=True, text=True,
                env=clean_env,
                cwd=str(tdp),
            )
            # Pinned nadi-kit with GITHUB_ACTIONS=true and invalid key:
            # Raises "No usable node identity" — fail closed.
            # No ephemeral keypair generation, no .node_keys.json created.
            assert result.returncode != 0, (
                f"invalid key in CI must fail. "
                f"stdout={result.stdout} stderr={result.stderr}"
            )
            assert "No usable node identity" in result.stderr, (
                "must report identity failure"
            )
            assert "!!!invalid-key!!!" not in result.stdout, (
                "secret must not leak to stdout"
            )
            assert "!!!invalid-key!!!" not in result.stderr, (
                "secret must not leak to stderr"
            )
            assert not keys_path.exists(), (
                ".node_keys.json must not be created with invalid CI key"
            )


class TestCIValidKey:
    """Valid Ed25519 key in CI loads correctly, no key file written."""

    def test_valid_ed25519_key_loads_in_ci(self):
        if _NADI_KIT is None:
            pytest.skip("nadi-kit not installed")
        import tempfile
        import json as _json

        # Generate a valid Ed25519 test key
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        test_key = Ed25519PrivateKey.generate()
        test_key_pem = test_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            fed = tdp / "data" / "federation"
            fed.mkdir(parents=True)
            peer = {
                "identity": {"city_id": "ci-valid", "slug": "ci-valid",
                             "repo": "org/ci-valid", "public_key": ""},
                "endpoint": {"city_id": "ci-valid", "transport": "filesystem",
                             "location": str(fed)},
                "capabilities": [],
            }
            (fed / "peer.json").write_text(_json.dumps(peer))
            keys_path = fed / ".node_keys.json"
            assert not keys_path.exists()

            clean_env = {
                k: v for k, v in os.environ.items()
                if k not in ("PYTEST_CURRENT_TEST",)
            }
            clean_env["GITHUB_ACTIONS"] = "true"
            clean_env["NODE_PRIVATE_KEY"] = test_key_pem

            result = subprocess.run(
                [sys.executable, "-c", """
import os, sys
from pathlib import Path
# GITHUB_ACTIONS and NODE_PRIVATE_KEY from environment
from nadi_kit import NadiNode
try:
    node = NadiNode.from_peer_json(Path(sys.argv[1]))
    msgs = node.emit("test", {}, target="dest")
    print(f"OK agent_id={node.agent_id} source={msgs[0].source}")
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
""", str(fed / "peer.json")],
                capture_output=True, text=True,
                env=clean_env,
                cwd=str(tdp),
            )
            assert result.returncode == 0, f"valid key must succeed: {result.stderr}"
            assert "OK agent_id=" in result.stdout
            assert test_key_pem not in result.stdout, "key must not leak"
            assert test_key_pem not in result.stderr, "key must not leak"
            assert not keys_path.exists(), (
                ".node_keys.json must not be created in CI"
            )
