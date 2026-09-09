# Contributing to flashgate

Bug reports, board profiles, probes, adapters, and docs are all welcome.
This is a hardware project: the best contributions come with a real board
on your desk.

## Ways to contribute

- **Board profiles** (`boards/*.yaml`) — the highest-value contribution.
  Port the profile to a board you own, verify on real hardware
  (`flashgate verify --all-probes` exits 0), and open a PR with the
  profile + a short note on what the board needed. See `docs/GUIDE.md`
  §6 for the firmware-side integration recipe.
- **Bug reports** — include the exact command, the exit code, and the
  output tail. `flashgate doctor` output helps too.
- **Fixes and features** — see the development setup below.

## Development setup

```bash
git clone https://github.com/Lion-1209/flashgate
cd flashgate
pip install -e ".[dev,mcp]"   # dev = pytest; mcp = MCP contract tests
pytest -q                      # 60+ tests, no hardware needed
```

The test suite is hardware-free (serial/debugger calls are mocked in
unit tests; hardware runs are a separate manual step). If your change
touches `verify`/`flash`/`probe` behavior, say in the PR whether you ran
it against a real board.

## Pull requests

1. One feature/fix per PR.
2. `pytest -q` green before you push.
3. If you changed probe/banner/signature semantics, update the affected
   tests and `docs/GUIDE.md` in the same PR.
4. New public behavior needs an exit-code / envelope-code decision —
   check `flashgate/results.py` and the exit-code table in the README
   before inventing a new failure mode.

## Contribution terms

By submitting a pull request, or posting code, docs, or any other
contribution to this repository, you agree that:

1. Your contribution is licensed to the project under the repository's
   current license (MIT), **and**
2. You grant the project owner (Lion / Lion-1209) a perpetual,
   irrevocable, worldwide, royalty-free, non-exclusive right to use,
   reproduce, modify, sublicense (including under other licenses), and
   distribute your contribution as part of this project or products
   based on it, **and**
3. You confirm you have the legal right to submit it (your own work, or
   work you are authorized to contribute). If you contribute in the
   course of employment, you confirm your employer approves this
   submission.

Bug-report material (logs, traces, photos) attached to issues is used
only to reproduce and fix problems. You keep the copyright to your
contribution. These terms keep the project permissively licensed while
preserving the owner's ability to evolve licensing later (e.g.
dual-licensing around future commercial components). If you need your
contribution handled under different terms, say so in the issue/PR
**before** submitting and we'll work it out — most cases are fine.
