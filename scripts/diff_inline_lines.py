"""
Map git unified-diff '+' lines to 1-based line numbers in the NEW file, and snap
review thread JSON to those lines when the model picks the wrong row.
"""
from __future__ import annotations

import re
from typing import Dict, List

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT = re.compile(r"^diff --git a/(.+?) b/(.+?)$")


def _norm_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    return p if p.startswith("/") else f"/{p}"


def parse_git_diff_added_lines(diff_text: str) -> Dict[str, Dict[int, str]]:
    """
    For each file, map 1-based line number (new file) -> text for lines that appear
    as '+' additions in the unified diff (not context lines).
    """
    by_path: Dict[str, Dict[int, str]] = {}
    current: str | None = None
    in_hunk = False
    new_line = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_GIT.match(line)
            if m:
                current = _norm_path(m.group(2))
            in_hunk = False
            continue
        if line.startswith("+++ b/"):
            current = _norm_path(line[6:].strip())
            continue
        m = _HUNK.match(line)
        if m:
            in_hunk = True
            new_line = int(m.group(3))
            continue
        if not in_hunk or current is None:
            continue
        if not line:
            continue
        prefix = line[0]
        body = line[1:]
        if prefix == "+":
            if current not in by_path:
                by_path[current] = {}
            by_path[current][new_line] = body
            new_line += 1
        elif prefix == " ":
            new_line += 1
        elif prefix == "-":
            continue
        elif prefix == "\\":
            continue
        else:
            in_hunk = False

    return by_path


def _infer_line_from_comment(comment: str, pmap: Dict[int, str]) -> int | None:
    tags = list(dict.fromkeys(re.findall(r"@[A-Za-z0-9_]+", comment)))
    tags.sort(key=len, reverse=True)
    for tag in tags:
        matches = [ln for ln, tx in pmap.items() if tag in tx]
        if len(matches) == 1:
            return matches[0]
    return None


def snap_thread_lines_to_diff_adds(items: List[dict], diff_text: str) -> List[dict]:
    """Adjust 'line' on thread items when a unique '+' line matches @tags in comment."""
    if not diff_text.strip():
        return items
    added = parse_git_diff_added_lines(diff_text)
    out: List[dict] = []
    for it in items:
        path = it.get("path")
        comment = it.get("comment", "")
        if not isinstance(path, str) or not isinstance(comment, str) or not path.strip():
            out.append(it)
            continue
        p = _norm_path(path)
        pmap = added.get(p)
        if not pmap:
            out.append(it)
            continue
        inferred = _infer_line_from_comment(comment, pmap)
        if inferred is not None and inferred != it.get("line"):
            print(
                f"Snap inline line for {p}: {it.get('line')} -> {inferred} "
                f"(matched '+' line from diff)",
                flush=True,
            )
            row = dict(it)
            row["line"] = inferred
            out.append(row)
        else:
            out.append(it)
    return out
