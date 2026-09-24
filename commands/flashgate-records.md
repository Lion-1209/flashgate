---
description: Export verification records as a sendable package (with optional redaction)
---

The evidence archive lives at `<firmware>/.flashgate/records/`. List what
is retained (newest first), then export one record — or all of them — as
a file the user can forward to vendor support:

```bash
flashgate records                                        # what is retained
flashgate records --export case.md                       # newest, markdown
flashgate records --export case.json                     # newest, JSON
flashgate records --export case.md --all                 # every record
flashgate records --export case.md --redact              # scrub local identity
```

The export is a rendering of what the records already contain — no new
verdicts: per-check results, the `coverage` block (what this verdict
proves and what it does NOT), and the board's own words (banner line, SWD
signature, console tail, failing probe transcript).

`--redact` replaces the workstation's paths, hostname and username with
`<PATH>` / `<HOME>` / `<HOST>` / `<USER>` placeholders, keeping only file
basenames — the same scrubber `doctor --export --redact` uses. The test
suite asserts the redacted export greps clean, so a regression there is
red, not silent.

Exit 6 (with the reason) when there is nothing to export, the target
cannot be written, or the target is a Windows device name such as NUL —
never a phantom "exported" with no file. A record that cannot be parsed
is named in the header rather than dropped. Write the export OUTSIDE the
firmware repo: an untracked file inside a watched tree changes the tree
fingerprint and invalidates the Stop hook's cached PASS.
