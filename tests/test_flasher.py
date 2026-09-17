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


class TestWrite32Hotplug:
    """The wipe must attach WITHOUT resetting the target and use the
    -w32 primitive: the file-based --write is a flash operation that
    silently no-ops on RAM, and a Normal (resetting) connect lets the
    rebooting firmware re-publish the signature over the wipe (both
    proven on real hardware 2026-09-17)."""

    @staticmethod
    def _capture(monkeypatch, returncode=0, out="32-bit data download complete"):
        calls = []

        class P:
            pass
        p = P()
        p.returncode = returncode
        p.stdout, p.stderr = out, ""
        monkeypatch.setattr(flasher, "find_cubeprogrammer", lambda: "cli.exe")
        monkeypatch.setattr(flasher.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd) or p)
        return calls

    def test_uses_w32_primitive_and_hotplug(self, monkeypatch):
        calls = self._capture(monkeypatch)
        assert flasher.write32("port=SWD", 0, 0x2001FF00) is True
        cmd = calls[0]
        i = cmd.index("-w32")
        assert cmd[i + 1] == "0x2001ff00" and cmd[i + 2] == "0x00000000"
        assert cmd[cmd.index("--connect") + 1] == "port=SWD mode=Hotplug"

    def test_explicit_mode_respected(self, monkeypatch):
        calls = self._capture(monkeypatch)
        flasher.write32("port=SWD mode=Normal", 0, 0x2001FF00)
        assert calls[0][calls[0].index("--connect") + 1] == "port=SWD mode=Normal"

    def test_success_gated_on_download_message(self, monkeypatch):
        self._capture(monkeypatch, returncode=0, out="something else entirely")
        assert flasher.write32("port=SWD", 0, 0x2001FF00) is False

    def test_read_ram_also_attaches_hotplug(self, monkeypatch):
        from flashgate import swdsig
        calls = []
        import tempfile
        from pathlib import Path

        def fake_run(cmd, **kw):
            calls.append(cmd)
            # honor the --read output file argument
            out_path = Path(cmd[-1])
            out_path.write_bytes(b"\x00" * 4)
            class P: pass
            p = P(); p.returncode = 0; p.stdout, p.stderr = "", ""
            return p
        import subprocess as real_subprocess
        monkeypatch.setattr(real_subprocess, "run", fake_run)
        monkeypatch.setattr(swdsig, "find_cubeprogrammer", lambda: "cli.exe")
        got = swdsig.read_ram("port=SWD", 0x2001FF00, 4)
        assert got == b"\x00\x00\x00\x00"
        assert calls[0][calls[0].index("--connect") + 1] == "port=SWD mode=Hotplug"
