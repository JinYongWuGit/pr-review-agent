"""
Resolve Azure DevOps pull request context from a browser PR URL.

Fetches PR metadata via REST and prints KEY=value lines for bash (or VSO setvariable).

Environment:
  ADO_ACCESS_TOKEN / SYSTEM_ACCESSTOKEN   Bearer token for ADO Git API

Usage:
  python resolve_pr_from_url.py <pr-url> [--format shell|vso]

Example URL:
    https://dev.azure.com/your-org/your-project/_git/your-repo/pullrequest/123456
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None

from ssl_trust import configure_ssl

PR_URL_RE = re.compile(
    r"^https://dev\.azure\.com/"
    r"(?P<org>[^/]+)/"
    r"(?P<project>[^/]+)/"
    r"_git/(?P<repo>[^/]+)/"
    r"pullrequests?/(?P<pr_id>\d+)",
    re.IGNORECASE,
)


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


def parse_pr_url(url: str) -> dict[str, str]:
    raw = url.strip()
    if not raw:
        raise ValueError("PR URL is empty.")
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    raw = raw.rstrip("/")
    m = PR_URL_RE.match(raw)
    if not m:
        raise ValueError(
            "Invalid PR URL. Expected:\n"
            "  https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{id}"
        )
    pr_id = m.group("pr_id")
    return {
        "org": m.group("org"),
        "project": m.group("project"),
        "repo": m.group("repo"),
        "pr_id": pr_id,
    }


def _ref_branch_name(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return ref


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def resolve_pr(url: str, token: str) -> dict[str, str]:
    parsed = parse_pr_url(url)
    org = parsed["org"]
    project = parsed["project"]
    repo_name = parsed["repo"]
    pr_id = parsed["pr_id"]

    api_url = (
        f"https://dev.azure.com/{_quote(org)}/{_quote(project)}"
        f"/_apis/git/repositories/{_quote(repo_name)}/pullRequests/{pr_id}"
        f"?api-version=7.1"
    )
    data = _get_json(api_url, token)

    repository = data.get("repository") or {}
    repo_id = repository.get("id") or repo_name
    pr_title = str(data.get("title") or "").strip()
    target_ref = str(data.get("targetRefName") or "refs/heads/develop")
    source_ref = str(data.get("sourceRefName") or "")
    source_commit = ""
    lms = data.get("lastMergeSourceCommit")
    if isinstance(lms, dict):
        source_commit = str(lms.get("commitId") or "")

    if not source_ref and not source_commit:
        raise ValueError("PR has no source ref or lastMergeSourceCommit.")

    threads_url = (
        f"https://dev.azure.com/{_quote(org)}/{_quote(project)}"
        f"/_apis/git/repositories/{_quote(str(repo_id))}/pullRequests/{pr_id}"
        f"/threads?api-version=7.1"
    )
    # Browser-style path segments (no URL-encoding); used for git clone/fetch only.
    remote_url = f"https://dev.azure.com/{org}/{project}/_git/{repo_name}"

    return {
        "ADO_ORG": org,
        "ADO_PROJECT": project,
        "ADO_REPO_ID": str(repo_id),
        "ADO_REPO_NAME": repo_name,
        "ADO_PR_ID": pr_id,
        "ADO_THREADS_URL": threads_url,
        "PR_TITLE": pr_title,
        "PR_TARGET_BRANCH": _ref_branch_name(target_ref),
        "PR_SOURCE_REF": source_ref,
        "PR_SOURCE_COMMIT": source_commit,
        "REPO_REMOTE_URL": remote_url,
        "PR_REVIEW_ON_DEMAND": "1",
    }


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def emit(ctx: dict[str, str], fmt: str) -> None:
    for key, value in ctx.items():
        if fmt == "vso":
            print(f"##vso[task.setvariable variable={key}]{value}")
        else:
            print(f"{key}={_shell_quote(value)}")


def main() -> None:
    load_dotenv()
    configure_ssl()
    parser = argparse.ArgumentParser(description="Resolve ADO PR URL to env vars")
    parser.add_argument("pr_url", help="Browser PR URL on dev.azure.com")
    parser.add_argument(
        "--format",
        choices=("shell", "vso"),
        default="shell",
        help="Output shell KEY='value' lines or Azure Pipelines setvariable commands",
    )
    args = parser.parse_args()
    try:
        ctx = resolve_pr(args.pr_url, _token())
    except (ValueError, urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"resolve_pr_from_url: {e}", file=sys.stderr)
        sys.exit(1)
    emit(ctx, args.format)
    print(f"Resolved PR #{ctx['ADO_PR_ID']} in {ctx['ADO_REPO_NAME']}", file=sys.stderr)


if __name__ == "__main__":
    main()
