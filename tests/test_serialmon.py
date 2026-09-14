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
