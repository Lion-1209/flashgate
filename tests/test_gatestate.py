"""Tree fingerprint and watched-path filtering over a real git repo."""

from flashgate import gatestate


class TestWatchedPaths:
    def test_watch_filter(self, git_repo):
        (git_repo / "App" / "extra.c").write_text("int x;\n")
        (git_repo / "readme2.md").write_text("docs\n")
        watched = gatestate.watched_paths(git_repo, ["*.c", "*.h"])
        assert "App/extra.c" in watched
        assert all(w.endswith((".c", ".h")) for w in watched)

    def test_no_changes(self, git_repo):
        assert gatestate.watched_paths(git_repo, ["*.c"]) == []


class TestTreeFingerprint:
    def test_stable_when_unchanged(self, git_repo):
        a = gatestate.tree_fingerprint(git_repo)
        b = gatestate.tree_fingerprint(git_repo)
        assert a == b

    def test_changes_with_content(self, git_repo):
        before = gatestate.tree_fingerprint(git_repo)
        (git_repo / "App" / "main.c").write_text("int main(void) { return 2; }\n")
        assert gatestate.tree_fingerprint(git_repo) != before

    def test_two_dirty_states_differ(self, git_repo):
        (git_repo / "App" / "main.c").write_text("/* a */\n")
        a = gatestate.tree_fingerprint(git_repo)
        (git_repo / "App" / "main.c").write_text("/* b */\n")
        assert gatestate.tree_fingerprint(git_repo) != a


class TestState:
    def test_roundtrip_and_missing(self, tmp_path, git_repo):
        assert gatestate.load_state(git_repo) == {}
        gatestate.save_state(git_repo, verified_fingerprint="abc", consecutive_blocks=0)
        s = gatestate.load_state(git_repo)
        assert s["verified_fingerprint"] == "abc"
        assert "updated" in s


class TestUntrackedContentFingerprint:
    def test_untracked_content_edit_changes_fingerprint(self, git_repo):
        # 2026-09-13 external-review finding: git status lists untracked
        # paths only, so editing an untracked file's CONTENT kept the tree
        # fingerprint unchanged — the Stop hook would reuse a cached PASS
        # over silently-modified code. Contents must be hashed in.
        from flashgate.gatestate import tree_fingerprint
        untracked = git_repo / "App" / "new.c"
        untracked.write_text("int x = 1;\n")
        fp1 = tree_fingerprint(git_repo)
        untracked.write_text("int x = 999; /* silently changed */\n")
        fp2 = tree_fingerprint(git_repo)
        assert fp1 != fp2, "untracked content edit MUST change the fingerprint"

    def test_untracked_appearance_changes_fingerprint(self, git_repo):
        from flashgate.gatestate import tree_fingerprint
        fp1 = tree_fingerprint(git_repo)
        (git_repo / "App" / "added.c").write_text("int y;\n")
        assert tree_fingerprint(git_repo) != fp1

    def test_tracked_edit_still_changes_fingerprint(self, git_repo):
        from flashgate.gatestate import tree_fingerprint
        fp1 = tree_fingerprint(git_repo)
        (git_repo / "App" / "main.c").write_text("int main(void){return 1;}\n")
        assert tree_fingerprint(git_repo) != fp1

    def test_clean_tree_fingerprint_stable(self, git_repo):
        from flashgate.gatestate import tree_fingerprint
        assert tree_fingerprint(git_repo) == tree_fingerprint(git_repo)


class TestUntrackedFingerprintCompleteness:
    """Residual holes found by the adversarial re-review of the first fix:
    untracked NEW directories (git status collapses to '?? dir/') and
    non-ASCII paths (core.quotepath escaping) must not hide content edits,
    and the gate's own state writes must not invalidate the PASS cache."""

    def test_new_directory_content_edit_changes_fingerprint(self, git_repo):
        from flashgate.gatestate import tree_fingerprint
        mod = git_repo / "NewModule"
        mod.mkdir()
        (mod / "mod.c").write_text("int f(void){return 1;}\n")
        fp1 = tree_fingerprint(git_repo)
        (mod / "mod.c").write_text("int f(void){return 999;}\n")
        fp2 = tree_fingerprint(git_repo)
        assert fp1 != fp2, "content edit inside an untracked NEW dir must be visible"

    def test_non_ascii_path_content_edit_changes_fingerprint(self, git_repo):
        from flashgate.gatestate import tree_fingerprint
        src = git_repo / "App" / "新模块.c"
        src.write_text("int a = 1;\n")
        fp1 = tree_fingerprint(git_repo)
        src.write_text("int a = 999; /* silently changed */\n")
        fp2 = tree_fingerprint(git_repo)
        assert fp1 != fp2, "non-ASCII path (quotepath escaping) must not hide edits"

    def test_state_dir_writes_do_not_change_fingerprint(self, git_repo):
        from flashgate.gatestate import save_state, tree_fingerprint
        save_state(git_repo, status="pass", fingerprint="x")   # state dir exists
        fp1 = tree_fingerprint(git_repo)
        save_state(git_repo, status="pass", fingerprint="y")   # timestamp moves
        assert tree_fingerprint(git_repo) == fp1, ".flashgate/ must stay out of the digest"

    def test_new_dir_source_triggers_watch(self, git_repo):
        from flashgate.gatestate import DEFAULT_WATCH, watched_paths
        (git_repo / "NewModule").mkdir()
        (git_repo / "NewModule" / "mod.c").write_text("int x;\n")
        assert watched_paths(git_repo, DEFAULT_WATCH) == ["NewModule/mod.c"]
