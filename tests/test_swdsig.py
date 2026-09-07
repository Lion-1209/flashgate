"""Signature parsing against hand-built 64-byte buffers."""

import struct
import zlib

import pytest

from flashgate.swdsig import SIG_MAGIC, SignatureLayoutError, parse_signature


def build_sig(git=b"abc1234", build=b"2026-08-31T08:00:00Z", magic=SIG_MAGIC,
              version=1) -> bytes:
    buf = bytearray(64)
    struct.pack_into("<I", buf, 0x00, magic)
    struct.pack_into("<H", buf, 0x04, version)
    struct.pack_into("<H", buf, 0x06, 1)
    buf[0x08:0x08 + len(git)] = git
    buf[0x18:0x18 + len(build)] = build
    struct.pack_into("<I", buf, 0x30, zlib.crc32(bytes(buf[:0x30])) & 0xFFFFFFFF)
    return bytes(buf)


class TestParseSignature:
    def test_valid(self):
        info = parse_signature(build_sig())
        assert info == {
            "version": 1, "flags": 1,
            "git": "abc1234", "build": "2026-08-31T08:00:00Z",
        }

    def test_dirty_suffix_survives(self):
        info = parse_signature(build_sig(git=b"abc1234-dirty"))
        assert info["git"] == "abc1234-dirty"

    def test_wrong_magic_rejected(self):
        assert parse_signature(build_sig(magic=0xDEADBEEF)) is None

    def test_wrong_crc_rejected(self):
        buf = bytearray(build_sig())
        buf[0x08] ^= 0xFF                       # corrupt payload after CRC
        assert parse_signature(bytes(buf)) is None

    def test_zero_buffer_rejected(self):
        assert parse_signature(bytes(64)) is None

    def test_short_buffer_rejected(self):
        assert parse_signature(build_sig()[:48]) is None

    def test_unknown_layout_version_rejected(self):
        # CRC-valid, magic-valid — but a layout generation this tool
        # refuses to decode must never be treated as evidence.
        with pytest.raises(SignatureLayoutError, match="version 2"):
            parse_signature(build_sig(version=2))

    def test_wait_for_signature_fails_fast_on_unknown_version(self, monkeypatch):
        # An alien layout is permanent: the poll must return immediately
        # with the reason instead of spinning to the timeout.
        from flashgate import swdsig

        monkeypatch.setattr(swdsig, "read_ram", lambda *a, **k: build_sig(version=9))
        info, err = swdsig.wait_for_signature("port=SWD", 0x2001FF00, 64, timeout_s=20)
        assert info is None
        assert "version 9 not supported" in err
