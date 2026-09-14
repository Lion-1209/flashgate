"""Stop hook wiring: the board profile must reach the fingerprint.

Gatestate-level tests prove profile edits change the fingerprint; this
file guards the CALLER. If hooks/flashgate_stop.py ever stops passing
board.yaml_path into tree_fingerprint, a cached PASS would survive edits
to the verification semantics (probe expectations, build command) and no
gatestate test would notice — this is the test that goes red.
"""

import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "flashgate_stop.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("flashgate_stop_under_test", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hook(git_repo, tmp_path, monkeypatch):
    mod = load_hook()
    # A watched change must exist, or the hook allows the stop before it
    # ever reaches the fingerprint comparison.
    (git_repo / "App" / "extra.c").write_text("int x;\n")
    profile = tmp_path / "board.yaml"
    profile.write_text("board: t\nprobes: [strict]\n")
    board = SimpleNamespace(
        firmware_dir=git_repo, watch_globs=("*",), yaml_path=profile,
    )
    monkeypatch.setattr(mod, "load_board", lambda p: board)
    monkeypatch.delenv("FLASHGATE_BOARD", raising=False)
    monkeypatch.setattr(sys, "argv", ["flashgate_stop.py"])
    return mod, board, profile


def run_stop(mod, monkeypatch, state):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(mod.gatestate, "load_state", lambda d: dict(state))
    return mod.main()


class TestProfileInvalidatesCache:
    def test_profile_edit_after_cached_pass_reruns_verify(
            self, hook, git_repo, monkeypatch):
        mod, board, profile = hook
        calls = []

        def fake_verify(board_arg):
            calls.append(board_arg)
            return 0, "PASS"

        monkeypatch.setattr(mod, "run_verify", fake_verify)
        saved = {}

        def fake_save(d, **fields):
            saved.update(fields)

        monkeypatch.setattr(mod.gatestate, "save_state", fake_save)

        # 1st stop: no cache → real verify → PASS cached (tree + profile).
        assert run_stop(mod, monkeypatch, {}) == 0
        assert len(calls) == 1
        cached = saved["verified_fingerprint"]

        # 2nd stop: identical tree AND profile → cache hit, no verify.
        assert run_stop(mod, monkeypatch,
                        {"verified_fingerprint": cached}) == 0
        assert len(calls) == 1

        # 3rd stop: firmware tree untouched, but the verification SEMANTICS
        # changed (profile edit) → the cached green must NOT be reused.
        profile.write_text("board: t\nprobes: [loose]\n")
        assert run_stop(mod, monkeypatch,
                        {"verified_fingerprint": cached}) == 0
        assert len(calls) == 2, "a profile edit must force a re-verify"
        assert saved["verified_fingerprint"] != cached
