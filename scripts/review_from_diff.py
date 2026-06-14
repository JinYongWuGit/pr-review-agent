"""
Send a git diff (from changes.txt) to the Foundry prompt agent; write Markdown to output.md.

Environment:
  PROJECT_ENDPOINT        Foundry project endpoint (required)
    PR_RVW_AGENT_NAME       Same name used in create_agent.py (default: pr-review-agent)
  PR_REVIEW_INLINE_JSON   If 1/true: ask the model for a trailing JSON array and write
                          review_threads.json for post_pr_review.py (ADO inline threads).
  PR_REVIEW_SNAP_THREAD_LINES  Default on: rewrite thread `line` from the diff's '+' rows
                          using @tokens in each comment (fixes wrong model line numbers).

Arguments (optional):
  1  path to changes file (default: ./changes.txt or CHANGES_PATH)
  2  path to output markdown (default: ./output.md or OUTPUT_PATH)

Guards:
  MAX_DIFF_BYTES  (default 200000)
  MAX_DIFF_LINES  (default 8000)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from ssl_trust import configure_ssl

from diff_inline_lines import snap_thread_lines_to_diff_adds

DEFAULT_MAX_BYTES = 200_000
DEFAULT_MAX_LINES = 8_000

_INLINE_JSON_SUFFIX = """

---

**Machine-readable output (required):** After your Markdown review above, append exactly one
markdown fenced code block whose language tag is `json`. Inside the fence, output **only** a
JSON array (no prose). Each element must be an object with:

- `severity` (string): `high`, `medium`, or `low` for this finding.
- `comment` (string): Markdown for that PR thread (one thread per object).
- `path` (string or null): Repo path from the diff with a leading slash (example: `/build/ExportModel.feature`).
  Use `null` for the PR-wide summary only. **Only use a non-null `path` when `severity` is `high`.**
  Medium and low findings must use `path: null` (they belong in the summary object only).
- `line` (integer or null): **1-based line in the PR source (new) file**, as in an editor on the
  branch being merged — **not** a line number printed inside the diff text, **not** the old file,
  **not** a count from the top of the hunk only.
  **How to compute it from this unified diff:** each hunk header looks like `@@ -oldStart,oldLen +newStart,newLen @@`.
  Start counting the **new** file at `newStart` for the first context (` `) or addition (`+`) line
  in that hunk; increment for each ` ` or `+` line; **do not** increment for `-` lines only.
  The `line` for a comment about a `+` line is the new-file line number **on that added row**
  before moving to the next line.
  Required when `path` is set; use `null` when `path` is null.

Put **one** PR-wide summary first with `"path": null`, `"line": null`, and `"severity": null`.
Its `comment` must include the overall summary **and** all **medium** and **low** findings
(use **High** / **Medium** / **Low** subsections). Then add one object per **high** severity
finding only (non-null `path` and `line`). Do **not** emit inline objects for medium or low.

Do **not** output a raw JSON array without the ```json fence. Inside JSON string values,
escape embedded double quotes as `\\"` (e.g. `cryptic \\"resource not found\\" error`).
"""

_PR_TITLE_PREFIX = """\
[Pull request title]
{title}

[Diff]
"""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _snap_thread_lines_enabled() -> bool:
    v = os.environ.get("PR_REVIEW_SNAP_THREAD_LINES", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _extract_json_fence(markdown: str) -> tuple[str | None, str]:
    """Return (inner_json_text, markdown_with_fence_removed)."""
    m = re.search(r"```json\s*([\s\S]*?)\s*```", markdown, flags=re.IGNORECASE)
    if not m:
        return None, markdown
    inner = m.group(1).strip()
    stripped = (markdown[: m.start()] + markdown[m.end() :]).strip()
    return inner, stripped


def _find_trailing_json_array(text: str) -> tuple[str | None, int]:
    """Last balanced `[...]` array in text; return (slice, start_index)."""
    i = len(text) - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0 or text[i] != "]":
        return None, -1
    end = i
    depth = 0
    for j in range(i, -1, -1):
        ch = text[j]
        if ch == "]":
            depth += 1
        elif ch == "[":
            depth -= 1
            if depth == 0:
                chunk = text[j : end + 1]
                if '"comment"' not in chunk and "'comment'" not in chunk:
                    return None, -1
                return chunk, j
    return None, -1


def _extract_trailing_json_array(markdown: str) -> tuple[str | None, str]:
    """Models often append a bare `[{...}]` block without a ```json fence."""
    text = markdown.rstrip()
    body = text
    marker_pos = body.rfind("[pr-review-agent]")
    if marker_pos >= 0:
        body = body[:marker_pos].rstrip()
    inner, start = _find_trailing_json_array(body)
    if not inner:
        return None, markdown
    stripped = body[:start].rstrip()
    return inner, stripped


def _extract_inline_threads(markdown: str) -> tuple[str | None, str]:
    inner, stripped = _extract_json_fence(markdown)
    if inner:
        return inner, stripped
    return _extract_trailing_json_array(markdown)


def _normalize_json_text(raw: str) -> str:
    return (
        raw.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


def _normalize_severity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if s.startswith("high"):
        return "high"
    if s.startswith("medium") or s.startswith("med"):
        return "medium"
    if s.startswith("low"):
        return "low"
    return None


def _is_summary_thread(item: dict) -> bool:
    path = item.get("path")
    return path is None or (isinstance(path, str) and not str(path).strip())


def _filter_threads_by_severity(items: list[dict]) -> list[dict]:
    """Keep summary plus high-severity inline threads only."""
    kept: list[dict] = []
    dropped = 0
    for item in items:
        comment = item.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            continue
        if _is_summary_thread(item):
            kept.append(
                {
                    "comment": comment.strip(),
                    "path": None,
                    "line": None,
                }
            )
            continue
        sev = _normalize_severity(item.get("severity"))
        if sev is None:
            comment_l = comment.lower()
            if re.search(r"\*\*high\*\*|^high[:\s-]", comment_l, re.MULTILINE):
                sev = "high"
            elif re.search(r"\*\*medium\*\*|^medium[:\s-]", comment_l, re.MULTILINE):
                sev = "medium"
            elif re.search(r"\*\*low\*\*|^low[:\s-]", comment_l, re.MULTILINE):
                sev = "low"
        if sev != "high":
            dropped += 1
            continue
        kept.append(
            {
                "comment": comment.strip(),
                "path": item.get("path"),
                "line": item.get("line"),
            }
        )
    if dropped:
        print(
            f"Omitted {dropped} medium/low inline thread(s); those belong in the summary only.",
            file=sys.stderr,
        )
    return kept


def _parse_threads_array(raw: str) -> list | None:
    text = raw.strip()
    for candidate in (text, _normalize_json_text(text)):
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    try:
        from json_repair import repair_json

        repaired = repair_json(_normalize_json_text(text))
        data = json.loads(repaired)
        if isinstance(data, list):
            return data
    except ImportError:
        print(
            "WARN: json-repair not installed; cannot repair malformed inline JSON.",
            file=sys.stderr,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"Could not parse inline JSON array: {e}", file=sys.stderr)
    return None


def _write_review_threads(
    inner: str, dest: Path, diff_text: str | None = None
) -> bool:
    data = _parse_threads_array(inner)
    if data is None:
        return False
    if not isinstance(data, list):
        print("JSON fence must contain an array.", file=sys.stderr)
        return False
    cleaned: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"Skipping non-object at index {i}", file=sys.stderr)
            continue
        comment = item.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            print(f"Skipping index {i}: missing comment", file=sys.stderr)
            continue
        cleaned.append(
            {
                "comment": comment.strip(),
                "path": item.get("path"),
                "line": item.get("line"),
                "severity": item.get("severity"),
            }
        )
    cleaned = _filter_threads_by_severity(cleaned)
    if not cleaned:
        print("No valid thread objects in JSON array.", file=sys.stderr)
        return False
    if diff_text and _snap_thread_lines_enabled():
        try:
            cleaned = snap_thread_lines_to_diff_adds(cleaned, diff_text)
        except Exception as e:
            print(f"WARN: snap thread lines from diff: {e}", file=sys.stderr)
    dest.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
    print(f"Wrote {len(cleaned)} thread(s) to {dest}")
    return True


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def _truncate_diff(text: str, max_bytes: int, max_lines: int) -> tuple[str, bool]:
    truncated = False
    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        text = "".join(lines)
        truncated = True
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="replace")
        truncated = True
    return text, truncated


def main() -> None:
    load_dotenv()
    configure_ssl()
    _require_env("PROJECT_ENDPOINT")
    agent_name = os.environ.get("PR_RVW_AGENT_NAME", "pr-review-agent").strip()
    pr_title = os.environ.get("PR_REVIEW_PR_TITLE", "").strip()

    changes_path = (
        (sys.argv[1] if len(sys.argv) > 1 else None)
        or os.environ.get("CHANGES_PATH", "changes.txt")
    )
    output_path = (
        (sys.argv[2] if len(sys.argv) > 2 else None)
        or os.environ.get("OUTPUT_PATH", "output.md")
    )

    max_bytes = int(os.environ.get("MAX_DIFF_BYTES", DEFAULT_MAX_BYTES))
    max_lines = int(os.environ.get("MAX_DIFF_LINES", DEFAULT_MAX_LINES))

    if not os.path.isfile(changes_path):
        msg = f"## PR review\n\nNo diff file at `{changes_path}` (empty PR or checkout issue).\n"
        with open(output_path, "w", encoding="utf-8") as out:
            out.write(msg)
        print(msg)
        return

    with open(changes_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    if not raw.strip():
        msg = "## PR review\n\nNo changes in diff — nothing to review.\n"
        with open(output_path, "w", encoding="utf-8") as out:
            out.write(msg)
        print(msg)
        return

    body, truncated = _truncate_diff(raw, max_bytes, max_lines)
    if truncated:
        banner = (
            "[Agent input truncated for size limits "
            f"(max {max_lines} lines, {max_bytes} bytes). "
            "Review may miss later files/hunks.]\n\n"
        )
        body = banner + body

    if pr_title:
        body = _PR_TITLE_PREFIX.format(title=pr_title) + body

    if _truthy("PR_REVIEW_INLINE_JSON"):
        body = body + _INLINE_JSON_SUFFIX

    project_client = AIProjectClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )
    openai_client = project_client.get_openai_client()

    conversation = openai_client.conversations.create()
    print(f"Created conversation (id: {conversation.id})")

    response = openai_client.responses.create(
        conversation=conversation.id,
        extra_body={
            "agent_reference": {"type": "agent_reference", "name": agent_name}
        },
        input=body,
    )
    text = response.output_text or ""
    print(f"Response output: {text[:500]}{'…' if len(text) > 500 else ''}")

    out_md = text
    threads_path = Path(output_path).with_name("review_threads.json")
    if _truthy("PR_REVIEW_INLINE_JSON"):
        inner, stripped = _extract_inline_threads(text)
        snap_diff = raw if _snap_thread_lines_enabled() else None
        if inner and _write_review_threads(inner, threads_path, diff_text=snap_diff):
            out_md = stripped
        elif inner:
            print(
                "Inline JSON present but invalid; summary will omit the JSON block.",
                file=sys.stderr,
            )
            out_md = stripped
        else:
            print(
                "PR_REVIEW_INLINE_JSON set but no ```json fence or trailing JSON array; "
                "post_pr_review.py will not run unless you add review_threads.json manually.",
                file=sys.stderr,
            )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(out_md)


if __name__ == "__main__":
    main()
