"""
Post Azure DevOps pull request review threads from a JSON file.

Each item becomes one PR thread. Items with "path" and "line" become **inline**
(code review) threads using threadContext (right-hand / source side of the PR).

Environment:
  ADO_ACCESS_TOKEN   Bearer token (PAT with Code (write) + vso.threads_full, or System.AccessToken)
  ADO_THREADS_URL    Full REST POST URL (must include /_apis/git/...), NOT the browser PR page URL.

  Alternatively build the URL from:
    ADO_ORG            Organization name (e.g. your-org)
    ADO_PROJECT        Project name (e.g. your-project) — required for multi-project orgs
  ADO_REPO_ID        Repository GUID or name
  ADO_PR_ID          Pull request numeric id
  ADO_API_VERSION    Optional, default 7.1

Arguments:
  1  Path to JSON file (default: review_threads.json next to cwd)

JSON format (array):
  [
    {"severity": null, "path": null, "line": null, "comment": "## Summary\\n... medium/low ..."},
    {"severity": "high", "path": "/build/ExportModel.feature", "line": 12, "comment": "..."}
  ]

- severity: high | medium | low (null on PR-wide summary). Only high may use path/line.
- path: repo path as in git diff, with leading slash (e.g. /build/foo.feature)
- line: 1-based line on the NEW (right) version of the file for that thread anchor
- comment: Markdown body for that thread (ADO commentType text = 1)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from pr_review_state import (
    THREAD_STATUS_ACTIVE,
    THREAD_STATUS_FIXED,
    append_marker,
    commit_sha_from_env,
    dedup_enabled,
    filter_duplicate_threads,
    norm_repo_path,
    post_marker_thread,
    should_skip_review,
)
from ssl_trust import configure_ssl


def _strip_env(s: str) -> str:
    return s.strip().strip('"').strip("'")


def _compose_threads_url_from_parts() -> str | None:
    org = _strip_env(os.environ.get("ADO_ORG", ""))
    project = _strip_env(os.environ.get("ADO_PROJECT", ""))
    repo = _strip_env(os.environ.get("ADO_REPO_ID", ""))
    pr_raw = _strip_env(os.environ.get("ADO_PR_ID", ""))
    api_ver = _strip_env(os.environ.get("ADO_API_VERSION", "7.1")) or "7.1"
    if not (org and project and repo and pr_raw):
        return None
    try:
        pr_num = int(pr_raw)
    except ValueError:
        print("ADO_PR_ID must be an integer.", file=sys.stderr)
        sys.exit(1)
    enc = lambda x: urllib.parse.quote(x, safe="")
    return (
        f"https://dev.azure.com/{enc(org)}/{enc(project)}"
        f"/_apis/git/repositories/{enc(repo)}/pullRequests/{pr_num}/threads"
        f"?api-version={urllib.parse.quote(api_ver, safe='')}"
    )


def _validate_threads_url(u: str) -> None:
    low = u.lower()
    if "/_git/" in low:
        print(
            "Error: URL looks like a browser git page (contains /_git/). "
            "Use the REST API path: …/_apis/git/repositories/{repoId}/pullRequests/{prId}/threads?api-version=7.1",
            file=sys.stderr,
        )
        sys.exit(1)
    if "/_apis/git/repositories/" not in low:
        print(
            "Error: ADO_THREADS_URL must contain `/_apis/git/repositories/`.\n"
            "Example:\n"
            "  https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repoId}"
            "/pullRequests/{prId}/threads?api-version=7.1",
            file=sys.stderr,
        )
        sys.exit(1)
    if "/pullRequests/" not in u:
        print(
            "Error: URL must contain the path segment `pullRequests` (camelCase), e.g.\n"
            "  …/repositories/<repoId>/pullRequests/<prId>/threads?api-version=7.1",
            file=sys.stderr,
        )
        sys.exit(1)
    if "threads" not in low.split("?")[0].rstrip("/").split("/")[-1].lower():
        print(
            "Error: URL path must end with `…/threads` (before the ?api-version= query).",
            file=sys.stderr,
        )
        sys.exit(1)


def _threads_url() -> str:
    u = _strip_env(os.environ.get("ADO_THREADS_URL", ""))
    if not u:
        u = _compose_threads_url_from_parts() or ""
    if not u:
        print(
            "Missing threads URL. Set ADO_THREADS_URL to the full POST …/threads URL,\n"
            "or set ADO_ORG, ADO_PROJECT, ADO_REPO_ID, ADO_PR_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    _validate_threads_url(u)
    return u


def _token() -> str:
    t = (
        os.environ.get("ADO_ACCESS_TOKEN", "").strip()
        or os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    )
    if not t:
        print(
            "Missing ADO_ACCESS_TOKEN or SYSTEM_ACCESSTOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)
    return t


def _build_thread_body(item: dict) -> dict:
    comment = item.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        raise ValueError("Each item needs a non-empty string 'comment'")
    path = norm_repo_path(item.get("path"))
    line = item.get("line")
    comments = [
        {"parentCommentId": 0, "content": comment.strip(), "commentType": 1}
    ]
    if path:
        ln = int(line) if line is not None else 1
        if ln < 1:
            raise ValueError(f"line must be >= 1, got {ln}")
        # ADO anchors on the right (source / PR) side of the comparison.
        # ADO rejects offset 0 ("outside of the allowed range"); use 1-based column start.
        ctx = {
            "filePath": path,
            "rightFileStart": {"line": ln, "offset": 1},
            "rightFileEnd": {"line": ln, "offset": 200},
        }
        return {"comments": comments, "threadContext": ctx, "status": THREAD_STATUS_ACTIVE}
    # PR-wide summary: Resolved so it does not block merge policies that require active threads cleared.
    return {"comments": comments, "status": THREAD_STATUS_FIXED}


def _is_pr_summary_item(item: dict) -> bool:
    path = item.get("path")
    return path is None or (isinstance(path, str) and not str(path).strip())


def _order_threads_for_post(items: list[dict]) -> list[dict]:
    """Inline first, PR-wide summary last — newest summary appears on top in the PR feed."""
    inline: list[dict] = []
    summaries: list[dict] = []
    for item in items:
        if _is_pr_summary_item(item):
            summaries.append(item)
        else:
            inline.append(item)
    return inline + summaries


def main() -> None:
    load_dotenv()
    configure_ssl()
    path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(os.getcwd(), "review_threads.json")
    )
    if not os.path.isfile(path):
        print(f"No thread file at {path}; nothing to post.", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        print("JSON root must be an array.", file=sys.stderr)
        sys.exit(1)
    url = _threads_url()
    token = _token()
    sha = commit_sha_from_env()
    if sha and dedup_enabled():
        try:
            repo_path = os.environ.get("PR_REPO_ROOT", "").strip() or None
            if should_skip_review(sha, url, token, repo_path=repo_path):
                print(
                    f"Commit {sha} already reviewed on this PR; skipping post.",
                    flush=True,
                )
                return
        except Exception as e:
            print(f"WARN: dedup check before post failed: {e}", file=sys.stderr)

    if dedup_enabled():
        before = len(data)
        data = filter_duplicate_threads(data, url, token)
        skipped = before - len(data)
        if skipped:
            print(
                f"Filtered {skipped} duplicate inline thread(s) already on this PR.",
                flush=True,
            )

    data = _order_threads_for_post(data)
    print(f"Posting threads to:\n  {url}", flush=True)
    summary_stamped = False
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"Skipping non-object at index {i}", file=sys.stderr)
            continue
        try:
            comment = item.get("comment")
            if (
                not summary_stamped
                and sha
                and item.get("path") in (None, "")
                and isinstance(comment, str)
            ):
                item = {**item, "comment": append_marker(comment, sha)}
                summary_stamped = True
            body = _build_thread_body(item)
        except ValueError as e:
            print(f"Index {i}: {e}", file=sys.stderr)
            sys.exit(1)
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            print(
                f"POST thread {i} failed: {e.code} {e.reason}\n"
                f"URL: {url}\n"
                f"{detail}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Posted thread {i + 1}/{len(data)}")

    if sha and not summary_stamped:
        try:
            post_marker_thread(url, token, sha)
        except Exception as e:
            print(f"WARN: could not post marker-only thread: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
