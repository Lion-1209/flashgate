"""wait_on evidence extraction: the matched_line is the BANNER line, not
whatever came after it in the same 512-byte chunk (adversarial F5)."""


class FakeConn:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _size: int) -> bytes:
        data, self._payload = self._payload, b""
        return data


class TestMatchedLine:
    def test_matched_line_is_the_banner_not_later_output(self):
        from flashgate import serialmon
        conn = FakeConn(b"FLASHGATE-BOOT board=b git=g\r\nLATER APP OUTPUT\r\n")
        res = serialmon.wait_on(
            conn, "FLASHGATE-BOOT board={board} git={git}", (), 1.0,
            echo=False)
        assert res.matched
        assert res.matched_line.startswith("FLASHGATE-BOOT")
        assert "LATER APP OUTPUT" not in res.matched_line

    def test_no_match_leaves_matched_line_empty(self):
        from flashgate import serialmon
        res = serialmon.wait_on(FakeConn(b"random noise\r\n"),
                                "BOOT git={git}", (), 0.2, echo=False)
        assert not res.matched and res.matched_line == ""


class TestOpenFlushRetry:
    """COM3 contention UX: transient access-denied (driver handle teardown
    after a just-exited process) retries instead of failing the gate;
    a genuinely held port exhausts attempts with actionable guidance."""

    @staticmethod
    def _denied():
        import serial as pyserial
        return pyserial.SerialException(
            "could not open port 'COM3': PermissionError(13, 'Access is denied.')")

    def test_transient_denied_then_success(self, monkeypatch):
        import serial as pyserial
        from flashgate import serialmon
        calls = {"n": 0, "slept": 0}
        monkeypatch.setattr(serialmon.time, "sleep",
                            lambda s: calls.__setitem__("slept", calls["slept"] + s))

        class FlakySerial:
            def __init__(self, *a, **kw):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise TestOpenFlushRetry._denied()

            def reset_input_buffer(self): pass
            def reset_output_buffer(self): pass

        monkeypatch.setattr(pyserial, "Serial", FlakySerial)
        conn = serialmon.open_flush("COM3", 115200)
        assert conn is not None
        assert calls["n"] == 3
        assert calls["slept"] == 2 * serialmon._OPEN_RETRY_DELAY_S

    def test_persistent_hold_exhausts_with_guidance(self, monkeypatch):
        import serial as pyserial
        import pytest
        from flashgate import serialmon
        monkeypatch.setattr(serialmon.time, "sleep", lambda s: None)

        class HeldSerial:
            def __init__(self, *a, **kw):
                raise TestOpenFlushRetry._denied()

        monkeypatch.setattr(pyserial, "Serial", HeldSerial)
        with pytest.raises(pyserial.SerialException) as ei:
            serialmon.open_flush("COM3", 115200)
        msg = str(ei.value)
        assert "held by another process" in msg
        assert "serial monitor" in msg and "retry" in msg

    def test_non_access_error_raises_immediately(self, monkeypatch):
        import serial as pyserial
        import pytest
        from flashgate import serialmon
        calls = {"n": 0}
        monkeypatch.setattr(serialmon.time, "sleep", lambda s: None)

        class NoPort:
            def __init__(self, *a, **kw):
                calls["n"] += 1
                raise pyserial.SerialException(
                    "could not open port 'COM99': FileNotFoundError(2, ...)")

        monkeypatch.setattr(pyserial, "Serial", NoPort)
        with pytest.raises(pyserial.SerialException):
            serialmon.open_flush("COM99", 115200)
        assert calls["n"] == 1, "non-access errors must not be retried"

    def test_first_try_success_no_sleep(self, monkeypatch):
        import serial as pyserial
        from flashgate import serialmon
        slept = []
        monkeypatch.setattr(serialmon.time, "sleep", slept.append)

        class OkSerial:
            def __init__(self, *a, **kw): pass
            def reset_input_buffer(self): pass
            def reset_output_buffer(self): pass

        monkeypatch.setattr(pyserial, "Serial", OkSerial)
        assert serialmon.open_flush("COM3", 115200) is not None
        assert slept == []


class TestConsoleCommandContract:
    """`flashgate console` on a held port: EXIT_ENV with the actionable
    message — never a raw traceback (runtime-audit G)."""

    def test_held_port_returns_env_not_traceback(self, tmp_path, monkeypatch):
        import serial as pyserial
        from types import SimpleNamespace
        from flashgate import cli
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COM3", "stub"))

        def held(*a, **kw):
            raise pyserial.SerialException(
                "COM3 is held by another process after 5 attempts (~2s)")

        monkeypatch.setattr(cli.serialmon, "console_forever", held)
        import pytest
        rc = cli.cmd_console(board)
        assert rc == cli.EXIT_ENV


class TestConsoleForeverRouting:
    """console_forever must actually go THROUGH open_flush (retry window,
    guidance) — a bypass back to a bare Serial survived green in the
    adversarial mutation round."""

    def test_console_forever_uses_open_flush(self, monkeypatch):
        from flashgate import serialmon
        seen = {}

        class FakeConn:
            def read(self, n):
                seen["reads"] = seen.get("reads", 0) + 1
                if seen["reads"] >= 2:
                    raise KeyboardInterrupt    # end the loop
                return b"x"

            def close(self):
                seen["closed"] = True

        def fake_open(device, baudrate, **kw):
            seen["open"] = (device, baudrate)
            return FakeConn()

        monkeypatch.setattr(serialmon, "open_flush", fake_open)
        try:
            serialmon.console_forever("COM3", 115200)
        except KeyboardInterrupt:
            pass
        assert seen["open"] == ("COM3", 115200)
        assert seen["closed"] is True
