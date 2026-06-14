"""
PR review idempotency: one Foundry review per commit (Build.SourceVersion).

Stores state as a marker line in PR summary comments:
    [pr-review-agent] reviewed-commit: <sha>

Environment:
  BUILD_SOURCEVERSION     Commit SHA for this build (set by pipeline)
  ADO_THREADS_URL         Same as post_pr_review.py
  ADO_ACCESS_TOKEN / SYSTEM_ACCESSTOKEN
  PR_REVIEW_SKIP_DEDUP    If 1/true: disable skip check (always review)

CLI:
  should-skip             Prints "yes" or "no" (stdout); exit 0
  stamp-summary <file>    Appends marker footer to a markdown file
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher

from dotenv import load_dotenv

from ssl_trust import configure_ssl

MARKER_PREFIX = "[pr-review-agent] reviewed-commit:"
# ADO CommentThreadStatus: active=1 blocks PR completion; fixed=2 is resolved.
THREAD_STATUS_ACTIVE = 1
THREAD_STATUS_FIXED = 2
MARKER_RE = re.compile(
    r"\[(?:pr-review-agent|pip-pr-review)\]\s+reviewed-commit:\s*([0-9a-fA-F]{7,40})",
    re.IGNORECASE,
)
MARKER_HTML_RE = re.compile(
    r"<!--\s*(?:pr-review-agent|pip-pr-review):sha=([0-9a-fA-F]{7,40})\s*-->",
    re.IGNORECASE,
)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def dedup_enabled() -> bool:
    return not _truthy("PR_REVIEW_SKIP_DEDUP")


def normalize_sha(sha: str) -> str:
    return sha.strip().lower()


def commit_sha_from_env() -> str | None:
    raw = (
        os.environ.get("BUILD_SOURCEVERSION", "").strip()
        or os.environ.get("Build_SourceVersion", "").strip()
    )
    if not raw:
        return None
    return normalize_sha(raw)


def marker_line(sha: str) -> str:
    return f"\n\n---\n{MARKER_PREFIX} {normalize_sha(sha)}\n"


def append_marker(content: str, sha: str) -> str:
    line = marker_line(sha)
    if MARKER_RE.search(content) or MARKER_HTML_RE.search(content):
        return content
    return content.rstrip() + line


def extract_shas_from_text(text: str) -> set[str]:
    found: set[str] = set()
    for pat in (MARKER_RE, MARKER_HTML_RE):
        for m in pat.finditer(text):
            found.add(normalize_sha(m.group(1)))
    return found


def _token() -> str:
    t = (
        os.environ.get("ADO_ACCESS_TOKEN", "").strip()
        or os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    )
    if not t:
        raise RuntimeError("Missing ADO_ACCESS_TOKEN or SYSTEM_ACCESSTOKEN.")
    return t


def _threads_list_url(post_url: str) -> str:
    base = post_url.split("?", 1)[0].rstrip("/")
    if not base.endswith("/threads"):
        raise ValueError("ADO_THREADS_URL must end with …/threads")
    api = "api-version=7.1"
    if "?" in post_url:
        q = post_url.split("?", 1)[1]
        if "api-version=" in q.lower():
            api = q
    return f"{base}?{api}"


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def iter_pr_threads(threads_post_url: str, token: str):
    """Yield each comment thread on the PR (paginated)."""
    url = _threads_list_url(threads_post_url)
    while url:
        data = _get_json(url, token)
        for thread in data.get("value") or []:
            if isinstance(thread, dict):
                yield thread
        url = data.get("nextLink") or ""


def reviewed_shas_on_pr(threads_post_url: str, token: str) -> set[str]:
    """Collect all commit SHAs recorded in PR review markers on this PR."""
    shas: set[str] = set()
    for thread in iter_pr_threads(threads_post_url, token):
        for comment in thread.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            content = comment.get("content")
            if isinstance(content, str):
                shas |= extract_shas_from_text(content)
    return shas


BUILD_SERVICE_AUTHOR_HINT = "Build Service"
DEFAULT_LINE_TOLERANCE = 6
COMMENT_SIMILARITY_THRESHOLD = 0.52


@dataclass(frozen=True)
class InlineSignature:
    path: str
    line: int
    comment: str
    topics: frozenset[str]


def norm_repo_path(path: str | None) -> str | None:
    if path is None:
        return None
    p = str(path).strip().replace("\\", "/")
    if not p:
        return None
    return p if p.startswith("/") else f"/{p}"


def _is_pipeline_review_comment(comment: dict) -> bool:
    author = comment.get("author")
    if not isinstance(author, dict):
        return False
    name = str(author.get("displayName") or "")
    return BUILD_SERVICE_AUTHOR_HINT in name


def _thread_inline_anchor(thread: dict) -> tuple[str, int] | None:
    ctx = thread.get("threadContext")
    if not isinstance(ctx, dict):
        return None
    path = norm_repo_path(ctx.get("filePath"))
    start = ctx.get("rightFileStart")
    if not path or not isinstance(start, dict):
        return None
    line = start.get("line")
    if not isinstance(line, int) or line < 1:
        return None
    return path, line


def _thread_first_text_comment(thread: dict) -> str | None:
    for comment in thread.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        if comment.get("commentType") == "system":
            continue
        content = comment.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def _is_marker_only_content(content: str) -> bool:
    if not MARKER_RE.search(content):
        return False
    stripped = content.strip()
    return stripped.startswith("AI PR review completed") or stripped.startswith(
        MARKER_PREFIX
    )


def _normalize_comment_text(text: str) -> str:
    s = re.sub(r"\s+", " ", text.lower())
    return re.sub(r"[^a-z0-9@._/-]+", " ", s).strip()


def _comments_similar(a: str, b: str, threshold: float = COMMENT_SIMILARITY_THRESHOLD) -> bool:
    na = _normalize_comment_text(a)
    nb = _normalize_comment_text(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


def _extract_topics(path: str, comment: str) -> frozenset[str]:
    topics: set[str] = set()
    c = comment.lower()
    if "testcatagory" in c or "testcategory" in c:
        topics.add("topic:test-tag-spelling")
    for m in re.finditer(r"refs/heads/[\w/.-]+", c, re.IGNORECASE):
        topics.add("ref:" + m.group(0).lower())
    if any(k in c for k in ("oauth", "system.accesstoken", "accesstoken")):
        topics.add("topic:oauth-token")
    if any(
        k in c
        for k in ("iam", "service principal", "foundry user", "permission", "blast radius")
    ):
        topics.add("topic:iam-permissions")
    if "hardcoded" in c or ("ref" in c and "branch" in c):
        topics.add("topic:pipeline-ref")
    if any(k in c for k in ("clutter", "smoke test", "unnecessary noise", "test purpose")):
        topics.add("topic:test-file-noise")
    if "random_notes" in c or "topsfolder" in c:
        topics.add("topic:topsfolder-test-file")
    topics.add("path:" + path.lower())
    return frozenset(topics)


def _topics_overlap(a: frozenset[str], b: frozenset[str]) -> bool:
    a_sem = {t for t in a if t.startswith("topic:") or t.startswith("ref:")}
    b_sem = {t for t in b if t.startswith("topic:") or t.startswith("ref:")}
    return bool(a_sem & b_sem)


def _lines_close(a: int, b: int, tolerance: int) -> bool:
    return abs(a - b) <= tolerance


def load_existing_inline_signatures(
    threads_post_url: str, token: str
) -> list[InlineSignature]:
    """Inline threads already posted by the PR review pipeline on this PR."""
    sigs: list[InlineSignature] = []
    for thread in iter_pr_threads(threads_post_url, token):
        anchor = _thread_inline_anchor(thread)
        if not anchor:
            continue
        path, line = anchor
        comment = _thread_first_text_comment(thread)
        if not comment or _is_marker_only_content(comment):
            continue
        comments = thread.get("comments") or []
        first = comments[0] if comments and isinstance(comments[0], dict) else None
        if first and not _is_pipeline_review_comment(first):
            continue
        sigs.append(
            InlineSignature(
                path=path,
                line=line,
                comment=comment,
                topics=_extract_topics(path, comment),
            )
        )
    return sigs


def is_duplicate_inline(
    path: str,
    line: int,
    comment: str,
    existing: list[InlineSignature],
    *,
    line_tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> bool:
    norm_path = norm_repo_path(path)
    if not norm_path:
        return False
    topics = _extract_topics(norm_path, comment)
    for sig in existing:
        if sig.path != norm_path:
            continue
        if not _lines_close(sig.line, line, line_tolerance):
            continue
        if _comments_similar(sig.comment, comment):
            return True
        if _topics_overlap(sig.topics, topics):
            return True
    return False


def filter_duplicate_threads(
    items: list[dict],
    threads_post_url: str,
    token: str,
) -> list[dict]:
    """Drop inline items that repeat an existing pipeline comment on this PR."""
    existing = load_existing_inline_signatures(threads_post_url, token)
    if not existing:
        return items
    kept: list[dict] = []
    for item in items:
        path = item.get("path")
        comment = item.get("comment")
        line = item.get("line")
        if isinstance(path, str) and isinstance(comment, str) and line is not None:
            try:
                ln = int(line)
            except (TypeError, ValueError):
                ln = 0
            if ln >= 1 and is_duplicate_inline(path, ln, comment, existing):
                norm_path = norm_repo_path(path)
                print(
                    f"Skip duplicate inline thread: {norm_path}:{ln} "
                    f"(similar comment already on PR)",
                    flush=True,
                )
                continue
        kept.append(item)
    return kept


def _is_git_ancestor(repo_path: str, ancestor: str, head: str) -> bool:
    try:
        subprocess.run(
            ["git", "-C", repo_path, "merge-base", "--is-ancestor", ancestor, head],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return True
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return False


def _commit_in_repo(repo_path: str, sha: str) -> bool:
    """True when sha resolves in the local clone (markers from ADO may be absent on shallow fetch)."""
    try:
        subprocess.run(
            ["git", "-C", repo_path, "cat-file", "-e", f"{normalize_sha(sha)}^{{commit}}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return True
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return False


def _diff_base_for_marker(marker_sha: str, head_sha: str, repo_path: str) -> str | None:
    """
    Git ref to use as the old side of an incremental diff (marker..HEAD).

    ADO PR builds sometimes record a merge commit in the marker; use the source
    parent (^2) when HEAD is the PR branch tip.
    """
    m = normalize_sha(marker_sha)
    h = normalize_sha(head_sha)
    if m == h:
        return m
    if not _commit_in_repo(repo_path, m):
        return None
    if _is_git_ancestor(repo_path, m, h):
        return m
    if _marker_matches_head(m, h, repo_path):
        for suffix in ("^2", "^1"):
            parent = _git_rev_parse(repo_path, f"{m}{suffix}")
            if parent and (_is_git_ancestor(repo_path, parent, h) or parent == h):
                return parent
        return m
    return None


def latest_reviewed_sha_for_diff(
    threads_post_url: str,
    token: str,
    head_sha: str,
    repo_path: str,
) -> str | None:
    """
    Most recently published PR review marker usable as incremental diff base.
    """
    head = normalize_sha(head_sha)
    dated: list[tuple[str, str]] = []
    for thread in iter_pr_threads(threads_post_url, token):
        content = _thread_first_text_comment(thread)
        if not content:
            continue
        shas = extract_shas_from_text(content)
        if not shas:
            continue
        published = str(thread.get("publishedDate") or "")
        for sha in shas:
            dated.append((published, normalize_sha(sha)))
    dated.sort(reverse=True)
    for _, sha in dated:
        base = _diff_base_for_marker(sha, head, repo_path)
        if base:
            return base
    return None


def _git_rev_parse(repo_path: str, ref: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_path, "rev-parse", ref],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return normalize_sha(out.strip())
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None


def _marker_matches_head(marker_sha: str, head_sha: str, repo_path: str | None) -> bool:
    """True if marker SHA equals HEAD or HEAD is a parent of an ADO PR merge commit marker."""
    m = normalize_sha(marker_sha)
    h = normalize_sha(head_sha)
    if m == h:
        return True
    if not repo_path or not os.path.isdir(os.path.join(repo_path, ".git")):
        return False
    if not _commit_in_repo(repo_path, m):
        return False
    for suffix in ("^1", "^2"):
        parent = _git_rev_parse(repo_path, f"{m}{suffix}")
        if parent and parent == h:
            return True
    return False


def should_skip_review(
    commit_sha: str,
    threads_post_url: str,
    token: str,
    repo_path: str | None = None,
    *,
    marker_shas: set[str] | None = None,
) -> bool:
    head = normalize_sha(commit_sha)
    shas = (
        marker_shas
        if marker_shas is not None
        else reviewed_shas_on_pr(threads_post_url, token)
    )
    for marker_sha in shas:
        if _marker_matches_head(marker_sha, head, repo_path):
            return True
    return False


def _post_thread_body(threads_post_url: str, token: str, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        threads_post_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def post_marker_thread(threads_post_url: str, token: str, sha: str) -> None:
    """Post a PR-wide thread that only records the reviewed commit (for dedup on re-run)."""
    content = (
        f"AI PR review completed for commit `{normalize_sha(sha)[:12]}…`.\n"
        + marker_line(sha).lstrip()
    )
    body = {
        "comments": [{"parentCommentId": 0, "content": content, "commentType": 1}],
        "status": THREAD_STATUS_FIXED,
    }
    _post_thread_body(threads_post_url, token, body)
    print(f"Posted pr-review-agent marker for commit {normalize_sha(sha)}", flush=True)


def cmd_should_skip() -> int:
    if not dedup_enabled():
        print("no", flush=True)
        return 0
    sha = commit_sha_from_env()
    if not sha:
        print("WARN: BUILD_SOURCEVERSION unset; cannot dedupe.", file=sys.stderr)
        print("no", flush=True)
        return 0
    post_url = os.environ.get("ADO_THREADS_URL", "").strip()
    if not post_url:
        print("WARN: ADO_THREADS_URL unset; cannot dedupe.", file=sys.stderr)
        print("no", flush=True)
        return 0
    try:
        token = _token()
        found = reviewed_shas_on_pr(post_url, token)
        print(
            f"Dedup check: current SHA={sha}; marker SHAs on PR={sorted(found) or '(none)'}",
            file=sys.stderr,
        )
        repo_path = os.environ.get("PR_REPO_ROOT", "").strip() or None
        if should_skip_review(
            sha, post_url, token, repo_path=repo_path, marker_shas=found
        ):
            print(
                f"Commit {sha} already has a pr-review-agent marker on this PR.",
                file=sys.stderr,
            )
            print("yes", flush=True)
            return 0
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as e:
        print(f"WARN: dedup check failed ({e}); proceeding with review.", file=sys.stderr)
    print("no", flush=True)
    return 0


def cmd_latest_reviewed_sha() -> int:
    if _truthy("PR_REVIEW_FULL_DIFF"):
        return 0
    head = commit_sha_from_env()
    post_url = os.environ.get("ADO_THREADS_URL", "").strip()
    repo_path = os.environ.get("PR_REPO_ROOT", "").strip()
    if not (head and post_url and repo_path):
        return 0
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        print("WARN: PR_REPO_ROOT is not a git repo; using full PR diff.", file=sys.stderr)
        return 0
    try:
        token = _token()
        last = latest_reviewed_sha_for_diff(post_url, token, head, repo_path)
        if last:
            print(last, flush=True)
            print(
                f"Incremental diff base: last reviewed commit {last}",
                file=sys.stderr,
            )
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as e:
        print(f"WARN: could not resolve last reviewed SHA ({e}); using full PR diff.", file=sys.stderr)
    return 0


def cmd_stamp_summary(path: str) -> int:
    sha = commit_sha_from_env()
    if not sha:
        print("WARN: BUILD_SOURCEVERSION unset; not stamping marker.", file=sys.stderr)
        return 0
    if not os.path.isfile(path):
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(append_marker(text, sha))
    print(f"Stamped review marker for commit {sha} on {path}")
    return 0


def main() -> None:
    load_dotenv()
    configure_ssl()
    if len(sys.argv) < 2:
        print(
            "Usage: pr_review_state.py should-skip | latest-reviewed-sha | stamp-summary <file>",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "should-skip":
        sys.exit(cmd_should_skip())
    if cmd == "latest-reviewed-sha":
        sys.exit(cmd_latest_reviewed_sha())
    if cmd == "stamp-summary":
        if len(sys.argv) < 3:
            print("Usage: pr_review_state.py stamp-summary <markdown-file>", file=sys.stderr)
            sys.exit(1)
        sys.exit(cmd_stamp_summary(sys.argv[2]))
    print(f"Unknown command: {cmd}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
