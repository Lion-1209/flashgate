# flashgate verification record — schema v1.0

Every `flashgate verify` run persists one JSON record under
`<firmware>/.flashgate/records/`. The filename is
`<UTC-ts>-<exit-code>-<fingerprint8>.json`, so `ls records/` reads like an
audit log. The MCP `verify` tool returns the same object inline as
`data.record`.

The record is the evidence layer of the gate: it answers, after the fact,
*what ran, on which tree and artifact, what the board actually said, and
which checks held*. A check that never executed appears as `skipped` —
never silently absent (the record-level echo of the gate's core rule).

## Fields

| field | type | meaning |
|---|---|---|
| `schema_version` | `"1.1"` | this format; breaking changes bump the major (1.1 added the `coverage` block) |
| `kind` | `"flashgate.verify"` | record type (room for other kinds later) |
| `record_id` | string | filename stem: `<ts>-<exit>-<fp8>` |
| `tool` | object | `name`, `version`, `python`, `platform` of the verifier |
| `board` | object | `name`, `mcu`, `profile` (path), `profile_sha256` |
| `firmware` | object | `dir`, `git_sha`, `tree_fingerprint`, `artifact`, and after a successful build: `artifact_sha256`, `artifact_bytes` |
| `run` | object | `mode` (`uart`\|`swd`), `probes` (requested list or null), `exit_code` (the CLI 0-7 contract), `status` (envelope word: succeeded/failed/timed_out/incomplete), `summary`, `started_at`, `finished_at`, `duration_ms` |
| `checks` | array | per-step verdicts, in plan order |
| `evidence` | array | raw material backing the verdicts |

## coverage (schema 1.1)

Every record carries a `coverage` block — the "what this PASS proves"
statement. A green record must never read as "all functionality passed":

| key | content |
|---|---|
| `statement` | fixed one-liner: the listed checks held on THIS board and bench for THIS tree — nothing beyond the list |
| `verified` | names of the checks that HELD (status `passed`) |
| `not_verified` | the standing blind spots: physical effects (register/console readbacks are the firmware's own software observations), everything outside the listed probes, environmental conditions; and "functional behavior entirely — no probes ran" whenever no probe passed |
| `profile_notes` | verbatim `coverage.notes` from the board profile — bench-specific caveats |
| `failed_checks` / `skipped_checks` | present only when applicable |

## checks[] entries

| field | meaning |
|---|---|
| `name` | `console` (uart mode), `build`, `flash`, `boot`, `identity`, `probe:<name>` |
| `status` | `passed` (executed, held), `failed` (executed, did not hold), `skipped` (never executed — with the reason in `detail`) |
| `detail` | short human line (build time, mismatch values, skip reason) |
| `duration_ms` | optional |

Plan order: uart runs are `console → build → flash → boot → identity →
probe:*`; swd runs drop `console`. Steps after the first failure are
`skipped` with `detail: "earlier step failed — not executed"`.

## evidence[] entries

| field | meaning |
|---|---|
| `kind` | `uart-banner` \| `swd-signature` \| `console-tail` \| `probe-transcript` |
| `source` | where it came from (`COM3`, `RAM@0x2001ff00`, probe name) |
| `content` | the raw material (matched banner line, parsed signature fields, last console output, failing step transcript); bounded to 4000 chars |

## Guarantees

- A record exists for **failed** runs, not only passing ones.
- Two identities, two granularities: `tree_fingerprint` identifies the
  SOURCE (stable across builds of one tree state);
  `artifact_sha256` identifies the BUILD (the firmware embeds its build
  timestamp, so two builds of one tree yield different artifacts —
  local and remote runs of the same tree match on fingerprint, not on
  artifact hash).
- `firmware.tree_fingerprint` is computed BEFORE the run — the same
  pre-run identity the Stop hook decides on (HEAD + tracked diff +
  untracked content + profile content + tool version) — so a record can
  be tied to the exact tree state the gate judged. A post-build
  fingerprint would diverge whenever the build drops untracked
  artifacts.
- Records live under `.flashgate/`, which is outside the fingerprint
  while the directory stays untracked/ignored: **add `.flashgate/` to
  your firmware repo's `.gitignore`** (the shipped example does). If a
  user tracks `.flashgate/` in git, every record write churns the
  fingerprint and the PASS cache stops hitting (fail-safe, wasteful).
- Record writing is auxiliary: an I/O failure is reported as a warning and
  never changes the verification outcome or exit code.
- Retention: at most the newest 500 records are kept; older ones are
  pruned automatically after each write (best-effort — a file that
  cannot be deleted is skipped).
- A run that CRASHES (unexpected exception) still leaves a record — a
  `verify` check entry names the exception — and returns exit 6, keeping
  the CLI contract instead of leaking a raw traceback exit code. A run
  cancelled with Ctrl+C leaves no record.
- `tool.version` comes from the installed package metadata. Running from
  a raw source tree (no installation) yields `0.0.0`.

## Example

```json
{
  "schema_version": "1.1",
  "kind": "flashgate.verify",
  "record_id": "20260914T071500123-7-1a2b3c4d",
  "tool": {"name": "flashgate", "version": "0.6.0", "python": "3.11.9", "platform": "win32"},
  "board": {"name": "apollo-h743", "mcu": "STM32H743IIT6",
            "profile": "boards/apollo-h743.yaml", "profile_sha256": "…"},
  "firmware": {"dir": "examples/apollo-h743", "git_sha": "297547c",
               "tree_fingerprint": "…", "artifact": "build/Debug/Apollo.bin",
               "artifact_sha256": "…", "artifact_bytes": 93432},
  "run": {"mode": "uart", "probes": ["all"], "exit_code": 7,
           "status": "failed", "summary": "functional probe failed",
           "started_at": "…", "finished_at": "…", "duration_ms": 8342},
  "checks": [
    {"name": "console", "status": "passed", "detail": "COM3 @ 115200"},
    {"name": "build",   "status": "passed", "detail": "OK in 2.7s, 0 warnings", "duration_ms": 2710},
    {"name": "flash",   "status": "passed", "detail": "written, verified, started"},
    {"name": "boot",    "status": "passed", "detail": "banner: FLASHGATE-BOOT board=… git=…"},
    {"name": "identity","status": "passed", "detail": "board=apollo-h743 git=297547c"},
    {"name": "probe:led-demo", "status": "failed",
     "detail": "timeout waiting for /OK led0 state=BREATH/ after 'led0 breath', last response: 'OK led0 state=OFF'"}
  ],
  "evidence": [
    {"kind": "uart-banner", "source": "COM3", "content": "FLASHGATE-BOOT board=apollo-h743 git=297547c …"},
    {"kind": "probe-transcript", "source": "led-demo", "content": "…"}
  ]
}
```
