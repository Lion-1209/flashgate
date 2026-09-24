"""Records export: the evidence archive as a sendable package (N1+N3).

`flashgate records --export <file> [--latest|--all] [--redact]` turns
verification records into one document a user can hand to vendor support:
what was verified, what the board actually said, and what the verdict
does NOT prove (the coverage block, carried verbatim from N1). Without
`--export` the same selection prints as a compact table so a user can
see which record they are about to send.

Redaction happens ONCE, before rendering, on the decoded record objects:
markdown and JSON are two renderers over the same scrubbed data, so they
cannot disagree about what was removed. The scrubber is the shared
`redact` util (the same one `doctor --export --redact` uses) — one
implementation, no divergent copies.

Scope note: the export carries no new verdicts. It re-packages evidence
that already exists; a record that could not be read is reported, never
silently dropped, and an empty selection is an error rather than an
empty file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, records as records_mod
from .mdsafe import md_block, md_cell

# The table view shows the newest N by default; the export has no cap —
# `--all` means every retained record, and the header says how many.
TABLE_LIMIT = 20


class ExportError(Exception):
    """The requested export could not be produced (nothing to export,
    unreadable target). Distinct from a verify verdict: this command
    never touches hardware."""
    pass


def _sorted_record_paths(fw_dir: Path) -> list[Path]:
    """Record files, newest first. Secondary key is the filename (its
    timestamp prefix): rapid writes land on the same mtime tick, and a
    glob-order tiebreak could show the newest record last (the same
    ordering rule records._prune uses)."""
    directory = records_mod.records_dir(fw_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"),
                  key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def select_records(fw_dir: Path, all_records: bool = False) -> list[Path]:
    """Newest record, or every retained record when `all_records`."""
    paths = _sorted_record_paths(fw_dir)
    return paths if all_records else paths[:1]


def load_selected(fw_dir: Path, all_records: bool = False
                  ) -> tuple[list[dict], list[str]]:
    """(records, unreadable filenames). A corrupt record is reported
    rather than skipped in silence — the export is evidence, and a gap
    in it must be visible."""
    loaded: list[dict] = []
    unreadable: list[str] = []
    for path in select_records(fw_dir, all_records):
        try:
            loaded.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            unreadable.append(path.name)
    return loaded, unreadable


def _one_line(record: dict) -> str:
    run = record.get("run", {})
    checks = record.get("checks", [])
    passed = sum(1 for c in checks if c.get("status") == "passed")
    return (f"exit={run.get('exit_code')} [{passed}/{len(checks)} checks] "
            f"{run.get('summary', '')}".strip())


def render_table(fw_dir: Path, limit: int = TABLE_LIMIT) -> str:
    """The no-argument view: which records exist, newest first."""
    paths = _sorted_record_paths(fw_dir)
    if not paths:
        return (f"no verification records under "
                f"{records_mod.records_dir(fw_dir)} — run "
                f"`flashgate verify` first")
    shown = paths[:limit]
    lines = [f"verification records — {records_mod.records_dir(fw_dir)}",
             f"{len(paths)} retained (newest {len(shown)} shown)"]
    for path in shown:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lines.append(f"  {path.name}  <unreadable>")
            continue
        run = record.get("run", {})
        board = record.get("board", {}).get("name", "?")
        git = (record.get("firmware", {}) or {}).get("git_sha", "?")
        lines.append(f"  {path.stem}")
        lines.append(f"      exit={run.get('exit_code')} "
                     f"{run.get('status', '?')}  board={board} git={git}  "
                     f"{run.get('summary', '')}")
    if len(paths) > len(shown):
        lines.append(f"  … and {len(paths) - len(shown)} older")
    lines.append("")
    lines.append("export one: flashgate records --export <file> "
                 "[--all] [--redact]")
    return "\n".join(lines)


def build_export(fw_dir: Path, all_records: bool, redact: bool
                 ) -> tuple[dict, list[str]]:
    """The export bundle: metadata plus the selected records, scrubbed
    when asked. Returns (bundle, unreadable filenames)."""
    loaded, unreadable = load_selected(fw_dir, all_records)
    if redact:
        from .redact import redact_json_document
        loaded = json.loads(redact_json_document(json.dumps(loaded)))
    bundle = {
        "export": {
            "tool": "flashgate",
            "version": __version__,
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "selector": "all" if all_records else "latest",
            "record_count": len(loaded),
            "unreadable": unreadable,
            "redacted": redact,
        },
        "records": loaded,
    }
    return bundle, unreadable


def render_json(bundle: dict) -> str:
    return json.dumps(bundle, indent=2, ensure_ascii=False)


def _record_section(record: dict, index: int, total: int) -> list[str]:
    run = record.get("run", {})
    board = record.get("board", {})
    firmware = record.get("firmware", {}) or {}
    coverage = record.get("coverage", {}) or {}
    lines = [
        f"## {index}/{total} — {md_cell(record.get('record_id', '?'))}",
        "",
        f"**exit {md_cell(run.get('exit_code'))} · "
        f"{md_cell(run.get('status', '?'))}"
        f" · {md_cell(run.get('summary', ''))}**",
        "",
        f"- 板卡：{md_cell(board.get('name', '?'))} "
        f"({md_cell(board.get('mcu', '?'))})",
        f"- 源码身份：git={md_cell(firmware.get('git_sha', '?'))} "
        f"tree_fingerprint={md_cell(firmware.get('tree_fingerprint', '?'))}",
    ]
    if firmware.get("artifact_sha256"):
        lines.append(
            f"- 构建身份：artifact_sha256="
            f"{md_cell(firmware['artifact_sha256'])} "
            f"({md_cell(firmware.get('artifact_bytes', '?'))} bytes)")
    # The two paths a support engineer actually asks for ("which profile,
    # which artifact") — and the two fields the redaction exists for, so
    # the markdown surface exercises the scrubber instead of rendering a
    # document that happens to contain no paths at all.
    if board.get("profile"):
        lines.append(f"- 档案：{md_cell(board['profile'])}")
    if firmware.get("artifact"):
        lines.append(f"- 产物：{md_cell(firmware['artifact'])}")
    tool = record.get("tool", {}) or {}
    if tool:
        lines.append(f"- 工具：{md_cell(tool.get('name', 'flashgate'))} "
                     f"{md_cell(tool.get('version', '?'))} "
                     f"(python {md_cell(tool.get('python', '?'))}, "
                     f"{md_cell(tool.get('platform', '?'))})")
    probes = run.get("probes")
    probes_text = ("、".join(str(p) for p in probes)
                   if isinstance(probes, list) and probes
                   else "(none)")
    lines.append(f"- 运行：mode={md_cell(run.get('mode', '?'))} "
                 f"probes={probes_text} "
                 f"duration_ms={md_cell(run.get('duration_ms', '?'))}")
    lines.append("")

    lines.append("| 检查项 | 结果 | 详情 |")
    lines.append("|---|---|---|")
    for c in record.get("checks", []):
        status = {"passed": "✅", "failed": "❌",
                  "skipped": "⏭️"}.get(c.get("status"), "⚠️")
        lines.append(f"| {md_cell(c.get('name', '?'))} | {status} "
                     f"| {md_cell(c.get('detail', ''))} |")
    lines.append("")

    if coverage:
        lines.append("### 这次结论证明了什么（coverage）")
        lines.append("")
        lines.append(md_cell(coverage.get("statement", "")))
        lines.append("")
        verified = coverage.get("verified") or []
        if verified:
            lines.append("- 已验证：" + "、".join(md_cell(v) for v in verified))
        for item in coverage.get("not_verified", []):
            lines.append(f"- 未验证：{md_cell(item)}")
        for item in coverage.get("profile_notes", []):
            lines.append(f"- 档案警示：{md_cell(item)}")
        for key, label in (("failed_checks", "失败项"),
                           ("skipped_checks", "未执行项")):
            names = coverage.get(key) or []
            if names:
                lines.append(f"- {label}：" + "、".join(md_cell(n)
                                                    for n in names))
        lines.append("")

    evidence = record.get("evidence", [])
    if evidence:
        lines.append("### 板子的原话（原始证据）")
        lines.append("")
        for e in evidence:
            # A paragraph header, NOT a list item: an indented code block
            # inside a list item needs the item's content column plus
            # four spaces, and four spaces there is only a lazy
            # paragraph continuation — the evidence would render as
            # inline markdown, so a hostile firmware could forge a
            # heading or a clickable link inside the export (runtime
            # audit, N3). At top level, four spaces IS a code block.
            lines.append(f"**{md_cell(e.get('kind', '?'))} "
                         f"← {md_cell(e.get('source', '?'))}**")
            lines.append("")
            lines.append(md_block(e.get("content", "")))
            lines.append("")
    return lines


def render_markdown(bundle: dict) -> str:
    """The sendable page: verdict-first summary, then one section per
    record with its checks, coverage block and raw evidence."""
    meta = bundle["export"]
    selected = bundle["records"]
    icon = "✅" if selected and all(
        r.get("run", {}).get("exit_code") == 0 for r in selected) else "❌"
    lines = [
        f"# flashgate 验证记录导出 — {icon}",
        "",
        f"- 生成时间（UTC）：{md_cell(meta['generated_at'])}",
        f"- flashgate 版本：{md_cell(meta['version'])}",
        f"- 选择范围：{md_cell(meta['selector'])}（{meta['record_count']} 份）",
        f"- 脱敏：{'已脱敏（本机路径/主机名/用户名已替换为占位符）' if meta['redacted'] else '未脱敏——含本机路径'}",
    ]
    if meta.get("unreadable"):
        lines.append(f"- 无法读取的记录："
                     + "、".join(md_cell(u) for u in meta["unreadable"]))
    lines.append("")
    lines.append("| 记录 | 退出码 | 结论 |")
    lines.append("|---|---|---|")
    for r in selected:
        run = r.get("run", {})
        lines.append(f"| {md_cell(r.get('record_id', '?'))} "
                     f"| {md_cell(run.get('exit_code'))} "
                     f"| {md_cell(run.get('summary', ''))} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    for i, record in enumerate(selected, 1):
        lines.extend(_record_section(record, i, len(selected)))
    lines.append("---")
    lines.append("此包由 `flashgate records --export` 生成；每份记录的 "
                 "coverage 段说明该结论的覆盖范围——启动成功不代表所有"
                 "功能通过。")
    return "\n".join(lines) + "\n"


def export_records(fw_dir: Path, path: Path, all_records: bool = False,
                   redact: bool = False) -> Path:
    """Write the bundle in the format the suffix names (.json -> JSON,
    anything else -> markdown). Raises ExportError when nothing can be
    exported or the file cannot be written."""
    paths = select_records(fw_dir, all_records)
    if not paths:
        raise ExportError(
            f"no verification records under "
            f"{records_mod.records_dir(fw_dir)} — run `flashgate verify` "
            f"first")
    bundle, _ = build_export(fw_dir, all_records, redact)
    text = (render_json(bundle) if path.suffix.lower() == ".json"
            else render_markdown(bundle))
    try:
        path.write_text(text, encoding="utf-8")
        size = path.stat().st_size if path.is_file() else 0
    except OSError as exc:
        raise ExportError(f"{path}: {exc}") from exc
    if not path.is_file() or not size:
        raise ExportError(
            f"{path}: nothing was written (device name or unwritable "
            f"target?)")
    return path
