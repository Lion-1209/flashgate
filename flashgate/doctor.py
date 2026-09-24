"""Doctor export: the environment health check as a sendable report.

`flashgate doctor --export <file>` turns the console checkup into a
standalone file (markdown by suffix `.md`, JSON by `.json`) a user can
hand to support — the "安装诊断器" deliverable, and the measuring stick
for the first-acceptance-median (≤30 min) decision gate. `--redact`
scrubs local paths/host identity via the shared redaction util (the
same one the records export uses).

The checkup logic lives here once: collect_report() produces plain
data; the console printer, the markdown page and the JSON dump are all
renderers over it, so the three surfaces cannot drift apart.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, backends, serialmon, swdsig
from .mdsafe import md_cell
from .sttools import augmented_env


class ExportError(Exception):
    """The report could not be written (unwritable path, permissions,
    disk). Distinct from a checkup verdict: the bench may be perfectly
    healthy while the export target is not."""
    pass


def _check(name, ok, detail, hint=""):
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


def collect_report(board) -> dict:
    """Run the whole checkup, return plain data (no printing)."""
    checks: list[dict] = []

    backend = backends.backend_for_board(board)
    exe = backend.available()
    if exe:
        checks.append(_check("backend", True,
                             f"{backend.name} ({exe})"))
    else:
        checks.append(_check(
            "backend", False, backend.name,
            f"install the {backend.name} toolchain or set its env var "
            "(see docs/GUIDE.md §2)"))

    if exe:
        listing = backend.discover()
        if backend.probe_detected(listing):
            checks.append(_check("probe", True, "detected"))
        else:
            # The listing IS the evidence: a broken tool and an unplugged
            # probe both read "not detected", but only one of them is
            # fixed by re-seating a USB cable. OpenOCD's discover()
            # deliberately returns the reason — throwing it away sent
            # people to check hardware that was fine (adversarial M-3).
            tail = listing.strip()[-200:] if listing.strip() else ""
            detail = "none detected"
            if tail:
                detail += f" — probe listing: {tail}"
            checks.append(_check(
                "probe", False, detail,
                "check the debug probe's USB cable, the board's power, "
                "and the probe driver — if the listing above names a "
                "tool failure, fix that first"))

    port, why = serialmon.resolve_console_port(
        board.serial_port, board.usb_vid, board.usb_pids)
    if port:
        checks.append(_check("console", True, f"{port} @ {board.baudrate}  [{why}]"))
    else:
        checks.append(_check(
            "console", False, f"UNRESOLVED — {why}",
            "plug the USB-TTL adapter, check the wiring (TX/RX swap is "
            "the classic), or set FLASHGATE_SERIAL_PORT explicitly"))

    env = augmented_env()
    for tool in ("cmake", "ninja", "arm-none-eabi-gcc"):
        found = shutil.which(tool, path=env.get("PATH"))
        if found:
            checks.append(_check(tool, True, found))
        else:
            checks.append(_check(
                tool, False, "not found in PATH or ST bundles",
                f"install {tool} or point PATH at the ST toolchain bundle"))

    checks.append(_check("head-sha", None, board.head_sha() or "unknown"))

    on_board = {"git": None, "build": None, "note": ""}
    try:
        info, err = swdsig.wait_for_signature(
            board.flash_connect, board.sig_address, board.sig_size,
            timeout_s=2.0, read_fn=backend.read_mem)
    except OSError as exc:
        # the read could not RUN at all (spawn failure, probe yanked
        # mid-read) — an environment fact, not a crash. Same class the
        # verify pipeline's readback path was hardened for (N0⑥): the
        # diagnostic tool must never traceback on its own subject.
        on_board["note"] = f"SWD read unavailable ({exc})"
        checks.append(_check("on-board", None,
                             f"SWD read unavailable ({exc})"))
    else:
        if info:
            on_board = {"git": info["git"], "build": info["build"],
                        "note": "SWD signature"}
            checks.append(_check(
                "on-board", True,
                f"git={info['git']} build={info['build']} (SWD signature)"))
        else:
            # A layout mismatch is NOT "old firmware": waiting longer
            # cannot fix a generation gap, and telling a user to reflash
            # when the tool is the stale side would be a lie. Neither is
            # a read that never ran — "CLI not found" is an environment
            # fact about the HOST, not a claim about the firmware, and
            # the old wording asserted one on a toolchain-less machine
            # (adversarial L-4).
            reason = (err or "").strip()
            if "not supported" in reason or "layout version" in reason:
                note = ("signature layout not understood by this "
                        "flashgate version")
            elif reason and reason != "no valid signature yet":
                note = f"SWD read unavailable ({reason})"
            else:
                note = "no SWD signature (old firmware?)"
            on_board["note"] = note
            checks.append(_check("on-board", None, note))

    problems = [c["detail"] for c in checks if c["ok"] is False]
    return {
        "tool": {"name": "flashgate", "version": __version__},
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "board": {
            "name": board.name,
            "mcu": board.mcu,
            "profile": str(board.yaml_path),
            "firmware_dir": str(board.firmware_dir),
            "artifact": str(board.artifact),
        },
        "checks": checks,
        "on_board": on_board,
        "problems": problems,
        "all_ok": not problems,
    }



def render_markdown(report: dict) -> str:
    """A standalone, sendable health page: verdict first, then every
    check with its fix hint, then identity/footer."""
    verdict = ("ALL CHECKS PASSED" if report["all_ok"]
               else f"{len(report['problems'])} PROBLEM(S) FOUND")
    icon = "✅" if report["all_ok"] else "❌"
    lines = [f"# flashgate 环境体检单 — {md_cell(report['board']['name'])}",
             "",
             f"**{icon} {verdict}**",
             "",
             f"- 生成时间（UTC）：{md_cell(report['generated_at'])}",
             f"- flashgate 版本：{md_cell(report['tool']['version'])}",
             f"- 板卡：{md_cell(report['board']['name'])} "
             f"({md_cell(report['board']['mcu'])})",
             f"- 档案：`{md_cell(report['board']['profile'])}`",
             "",
             "| 检查项 | 结果 | 详情 |",
             "|---|---|---|"]
    for c in report["checks"]:
        status = {True: "✅", False: "❌", None: "⚠️"}[c["ok"]]
        lines.append(f"| {md_cell(c['name'])} | {status} "
                     f"| {md_cell(c['detail'])} |")
    lines.append("")
    if report["problems"]:
        lines.append("## 问题与建议修复")
        for c in report["checks"]:
            if c["ok"] is False:
                hint = f" —— {md_cell(c['hint'])}" if c["hint"] else ""
                lines.append(f"- **{md_cell(c['name'])}**："
                             f"{md_cell(c['detail'])}{hint}")
        lines.append("")
    ob = report["on_board"]
    if ob.get("git") or ob.get("note"):
        note = (f"git={md_cell(ob['git'])} build={md_cell(ob['build'])}"
                if ob.get("git") else md_cell(ob.get("note", "")))
        lines.extend(["## 板上当前运行的固件", "",
                      f"{note}（体检时读到的实际状态）", ""])
    lines.extend(["---",
                  "此单由 `flashgate doctor --export` 生成；结论只覆盖上表"
                  "检查项，不代表固件功能验证。"])
    return "\n".join(lines) + "\n"


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)


def export_report(report: dict, path: Path, redact: bool = False) -> Path:
    """Write the report in the format the file suffix names (.json ->
    JSON, anything else -> markdown). `redact` scrubs local identity
    (home/host/user) via the shared redaction util. Returns the path.

    Raises ExportError when the file cannot be written — a silently
    missing export would be worse than a failed one: the user believes
    support has a report that was never produced."""
    if redact:
        from .redact import redact_json_document
        report = json.loads(redact_json_document(render_json(report)))
    text = (render_json(report) if path.suffix.lower() == ".json"
            else render_markdown(report))
    try:
        path.write_text(text, encoding="utf-8")
        # stat() INSIDE the try: a file deleted between write and check
        # would otherwise raise an uncaught OSError and escape as a
        # traceback + exit 1 (= "build failed", a lie) — adversarial L-C.
        size = path.stat().st_size if path.is_file() else 0
    except OSError as exc:
        raise ExportError(f"{path}: {exc}") from exc
    if not path.is_file() or not size:
        # Windows device names (NUL, CON) accept the write and produce no
        # file at all — rc=0 plus "exported" plus nothing on disk is
        # exactly the silent failure this path exists to prevent
        # (adversarial M-5).
        raise ExportError(
            f"{path}: nothing was written (device name or unwritable "
            f"target?)")
    return path
