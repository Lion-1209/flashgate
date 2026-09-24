"""Markdown-safe rendering for anything flashgate hands to a third party.

Both sendable surfaces render user-influenced strings into markdown: the
doctor checkup page (N2) and the verification-record export (N3). A value
that can carry a newline or a pipe must not be able to forge table rows
or whole sections, and link/image/HTML syntax must not survive either —
a clickable "[x](http://evil)" inside a forwarded report is a phishing
vector even though it cannot forge a verdict.

One definition, two importers: the escaping cannot drift between the two
surfaces the way the doctor/verify backend construction once did.
"""

from __future__ import annotations

# Escaped with a backslash so markdown renders them literally. Angle
# brackets are invalid in Windows filenames but perfectly legal in a
# board-profile string or in a signature field decoded from firmware RAM
# (errors="replace"), which is exactly where hostile values come from.
_ESCAPED = ("|", "[", "]", "<", ">")


def md_cell(text: object) -> str:
    """Make one string safe inside a markdown table cell or heading."""
    text = str(text)
    for ch in _ESCAPED:
        text = text.replace(ch, "\\" + ch)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return text.strip()


def md_block(text: object, indent: str = "    ") -> str:
    """Render one multi-line value as an indented markdown code block.

    Indentation rather than backticks: evidence content is firmware and
    console output, and a ``` fence inside it would close the block and
    let the rest of the transcript render as markdown.

    The caller MUST place the result after a blank line at TOP level —
    not inside a list item. An indented code block inside a list item
    needs the item's content column plus four spaces (six after "- "),
    and four spaces there is only a lazy paragraph continuation: the
    content then renders as inline markdown, which is exactly what this
    helper exists to prevent (runtime audit, N3)."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(indent + ln if ln else "" for ln in text.split("\n"))
