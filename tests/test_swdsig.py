"""Signature parsing against hand-built 64-byte buffers."""

import struct
import zlib

from flashgate.swdsig import SIG_MAGIC, parse_signature


def build_sig(git=b"abc1234", build=b"2026-08-31T08:00:00Z", magic=SIG_MAGIC) -> bytes:
    buf = bytearray(64)
    struct.pack_into("<I", buf, 0x00, magic)
    struct.pack_into("<H", buf, 0x04, 1)
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
