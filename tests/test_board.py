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

    def test_malformed_document_rejected_not_crash(self, tmp_path):
        # Adversarial review F4: an empty file parses to None and a
        # list/scalar document is equally malformed — both must raise
        # BoardError (the Stop hook's "gate misconfigured" branch), never
        # a bare AttributeError out of raw.get().
        import pytest
        for bad in ("", "- a\n- b\n", "just a scalar\n"):
            p = tmp_path / "bad.yaml"
            p.write_text(bad, encoding="utf-8")
            with pytest.raises(BoardError):
                load_board(p)


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


def test_coverage_notes_loaded(tmp_path):
    # N1 mutation gap E: the yaml coverage.notes -> Board.coverage_notes
    # chain had no guard — apollo's caveats could vanish from every
    # record with all tests green
    from flashgate.board import BoardError, load_board
    fw = tmp_path / "fw"
    fw.mkdir()
    p = tmp_path / "b.yaml"
    p.write_text(
        "board: t\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
        "  build: ninja\n  artifact: fw.bin\n"
        "serial:\n  banner: 'BOOT {git}'\n"
        "coverage:\n  notes:\n    - caveat one\n    - caveat two\n",
        encoding="utf-8")
    b = load_board(p)
    assert b.coverage_notes == ("caveat one", "caveat two")


def test_coverage_notes_scalar_rejected(tmp_path):
    # adversarial M3: a scalar would be iterated character-by-character
    # into every record's honesty field — refuse at load time
    import pytest
    from flashgate.board import BoardError, load_board
    fw = tmp_path / "fw"
    fw.mkdir()
    p = tmp_path / "b.yaml"
    p.write_text(
        "board: t\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
        "  build: ninja\n  artifact: fw.bin\n"
        "serial:\n  banner: 'BOOT {git}'\n"
        "coverage:\n  notes: some caveat\n",
        encoding="utf-8")
    with pytest.raises(BoardError, match="LIST of strings"):
        load_board(p)
