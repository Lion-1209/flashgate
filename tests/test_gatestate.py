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
