"""_kill_stlinkserver guards the flash-retry path across platforms.

External review (2026-09-13): the bare `taskkill` invocation raised
FileNotFoundError on Linux/macOS, crashing the retry that follows a failed
flash attempt — the retry loop must survive cleanup failures on any OS.
"""

from flashgate import flasher


class TestKillStlinkserver:
    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(flasher.sys, "platform", "linux")

        def boom(*args, **kwargs):
            raise AssertionError("no process may be spawned off Windows")

        monkeypatch.setattr(flasher.subprocess, "run", boom)
        flasher._kill_stlinkserver()          # must neither raise nor spawn

    def test_windows_invokes_taskkill(self, monkeypatch):
        monkeypatch.setattr(flasher.sys, "platform", "win32")
        calls = []
        monkeypatch.setattr(flasher.subprocess, "run",
                            lambda *a, **kw: calls.append(a) or None)
        flasher._kill_stlinkserver()
        assert calls, "on Windows the stale stlink-server must be killed"
        assert calls[0][0][:2] == ["taskkill", "/F"]

    def test_taskkill_failure_never_aborts_retry(self, monkeypatch):
        monkeypatch.setattr(flasher.sys, "platform", "win32")

        def boom(*args, **kwargs):
            raise OSError("blocked by policy")

        monkeypatch.setattr(flasher.subprocess, "run", boom)
        flasher._kill_stlinkserver()          # best-effort cleanup: swallow

    def test_taskkill_timeout_never_aborts_retry(self, monkeypatch):
        # Adversarial review F1: TimeoutExpired is a SubprocessError, not an
        # OSError — the retry loop must survive a kill that hangs 15 s too.
        monkeypatch.setattr(flasher.sys, "platform", "win32")

        def hang(*args, **kwargs):
            raise flasher.subprocess.TimeoutExpired(cmd="taskkill", timeout=15)

        monkeypatch.setattr(flasher.subprocess, "run", hang)
        flasher._kill_stlinkserver()
