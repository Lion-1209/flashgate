"""Board profile loading and the -dirty identity algorithm."""

from flashgate.board import BoardError, load_board


BODY = """
board: test-board
mcu: STM32F999
description: unit test fixture
firmware:
  dir: fw
  build: ninja -C build
  artifact: build/fw.bin
serial:
  baudrate: 9600
  banner_regex: 'BOOT git=(?P<git>\\S+)'
"""


def make_profile(tmp_path, body=BODY):
    fw = tmp_path / "fw"
    fw.mkdir(exist_ok=True)
    p = tmp_path / "board.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestLoadBoard:
    def test_fields_and_defaults(self, tmp_path):
        b = load_board(make_profile(tmp_path))
        assert b.name == "test-board"
        assert b.firmware_dir.name == "fw"
        assert b.artifact.name == "fw.bin"
        assert b.baudrate == 9600
        assert b.evidence_mode == "auto"          # default
        assert b.sig_address == 0x2001FF00        # default
        assert "*.c" in b.watch_globs             # DEFAULT_WATCH fallback

    def test_missing_firmware_dir_rejected(self, tmp_path):
        p = tmp_path / "lonely.yaml"
        p.write_text(BODY, encoding="utf-8")      # fw/ never created
        import pytest
        with pytest.raises(BoardError):
            load_board(p)

    def test_missing_required_key_rejected(self, tmp_path):
        body = BODY.replace("  build: ninja -C build\n", "")
        import pytest
        with pytest.raises(BoardError):
            load_board(make_profile(tmp_path, body))

    def test_missing_banner_rejected(self, tmp_path):
        body = BODY.replace(
            "  banner_regex: 'BOOT git=(?P<git>\\S+)'\n", "")
        import pytest
        with pytest.raises(BoardError):
            load_board(make_profile(tmp_path, body))

    def test_banner_template_key(self, tmp_path):
        body = BODY.replace(
            "  banner_regex: 'BOOT git=(?P<git>\\S+)'\n",
            "  banner: 'BOOT git={git}'\n")
        b = load_board(make_profile(tmp_path, body))
        assert b.banner_regex == "BOOT git={git}"


class TestHeadSha:
    def test_clean_then_dirty(self, tmp_path, git_repo):
        # reuse the git fixture as the firmware dir of a profile
        p = tmp_path / "board.yaml"
        p.write_text(
            BODY.replace("dir: fw", f"dir: {git_repo.name}"), encoding="utf-8")
        b = load_board(p)

        clean = b.head_sha()
        assert clean and not clean.endswith("-dirty")

        (git_repo / "App" / "main.c").write_text("int main(void) { return 1; }\n")
        dirty = b.head_sha()
        assert dirty.endswith("-dirty")
        assert dirty[:-6] == clean
