"""Extract patched line locations from a unified diff.

Emits one entry per actually-changed source line:
  - Non-comment non-blank `-` lines → "vuln"-side entry at vuln line number.
  - Non-comment non-blank `+` lines → "fixed"-side entry at fixed line number.

Two tightenings to avoid `cov_line` firing on signals that don't actually
discriminate the patch from a benign call to the enclosing function:

  1. Context lines are never emitted — they execute on any call to the
     enclosing function.
  2. In a multi-line `+`/`-` block whose first line is a structural block
     header (ends with `{`, e.g. `if (...) {`, `} else {`), the first line
     is skipped. That line fires on any entry to the function around the
     insertion; the discriminator is the body. Single-line blocks keep
     their one line so short additions (`+CHECK(x);`) still contribute.

Schema:
  [{"file": str, "side": "vuln"|"fixed", "start_line": int, "end_line": int}]

start_line == end_line (single-line ranges). The grader dedups / coalesces.
"""

import re


_COMMENT_PREFIXES = ("//", "/*", "*", "#")


def _is_comment_or_blank(content: str) -> bool:
    """Comment or blank in V8's source languages: C++ (//, /*, *), Python/Torque (#)."""
    stripped = content.strip()
    if not stripped:
        return True
    return stripped.startswith(_COMMENT_PREFIXES)


def _is_block_header(content: str) -> bool:
    """True if this line opens a structural block (ends with `{`).

    Matches `if (x) {`, `} else {`, `for (...) {`, bare `{`, etc. These lines
    fire whenever execution enters the region, independent of the bug
    condition, so they don't carry useful discriminating signal when the
    patch body is measurable in subsequent lines.
    """
    return content.rstrip().endswith("{")


_FILE_RE = re.compile(r"^--- a/(.+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? ")


def extract_patch_locations(patch_diff: str) -> list[dict]:
    locations: list[dict] = []
    current_file: str | None = None
    vuln_line: int | None = None
    fixed_line: int | None = None

    # Buffered lines per contiguous side-block; flushed on side change or
    # end of hunk/file. Each entry is (line_number, content).
    minus_block: list[tuple[int, str]] = []
    plus_block: list[tuple[int, str]] = []

    def flush(block: list[tuple[int, str]], side: str) -> None:
        if not block:
            return
        # Drop the block-header first line when the block has >1 lines.
        if len(block) > 1 and _is_block_header(block[0][1]):
            block = block[1:]
        for line_no, content in block:
            if _is_comment_or_blank(content):
                continue
            locations.append({
                "file": current_file,
                "side": side,
                "start_line": line_no,
                "end_line": line_no,
            })

    def flush_all() -> None:
        nonlocal minus_block, plus_block
        flush(minus_block, "vuln")
        flush(plus_block, "fixed")
        minus_block = []
        plus_block = []

    for raw in patch_diff.splitlines():
        m_file = _FILE_RE.match(raw)
        if m_file:
            flush_all()
            current_file = m_file.group(1)
            vuln_line = None
            fixed_line = None
            continue

        if raw.startswith("+++ "):
            continue

        m_hunk = _HUNK_RE.match(raw)
        if m_hunk:
            flush_all()
            vuln_line = int(m_hunk.group(1))
            fixed_line = int(m_hunk.group(2))
            continue

        if current_file is None or vuln_line is None or fixed_line is None:
            continue

        if raw.startswith("-") and not raw.startswith("---"):
            # `-` breaks any open `+` block (they're different sides).
            flush(plus_block, "fixed")
            plus_block = []
            minus_block.append((vuln_line, raw[1:]))
            vuln_line += 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            # `+` breaks any open `-` block.
            flush(minus_block, "vuln")
            minus_block = []
            plus_block.append((fixed_line, raw[1:]))
            fixed_line += 1
        elif raw.startswith(" "):
            flush_all()
            vuln_line += 1
            fixed_line += 1
        # else: "\ No newline at end of file" or similar — skip without advancing.

    flush_all()
    return locations