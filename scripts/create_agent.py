"""
Create or version a dedicated Azure AI Foundry prompt agent for ADO PR review.

Environment:
  PROJECT_ENDPOINT        Foundry project endpoint URL (required)
  PR_RVW_MODEL_DEPLOYMENT_NAME   Chat model deployment name in the project (required)
    PR_RVW_AGENT_NAME              Logical agent name (default: pr-review-agent)

Uses DefaultAzureCredential (Azure CLI, managed identity, etc.).
"""
from __future__ import annotations

import os
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from ssl_trust import configure_ssl

DEFAULT_INSTRUCTIONS = """You are a senior software engineer doing pull request reviews for a mixed-language repo (.NET/C#, PowerShell, JavaScript, HTML/CSS, Azure static web assets).

Focus on:
- Correctness risks, edge cases, and regression hazards
- Security (injection, secrets in code, authz/authn mistakes, unsafe defaults)
- Performance and unnecessary allocations or blocking I/O where obvious from the diff
- Maintainability (naming, structure, duplication, error handling)
- Dependency and platform drift when the diff touches packages or TFMs

Also check:
- Whether the pull request title accurately describes the actual changes implied by the diff.
  If the title seems misleading, too broad/narrow, or stale (e.g. copy/paste from another PR),
  call it out explicitly as a **Medium** finding and suggest a better title.

Output:
- Use clear Markdown with short sections and bullet points.
- Start with a 2–4 sentence summary of intent of the change (as inferred from the diff).
- Then "Findings" with subsections **High**, **Medium**, and **Low** (omit empty levels). If none, say so.
- End with "Residual risk / testing ideas" (concrete tests or checks), not generic platitudes.
- Label every finding with its severity (high / medium / low).
- **Inline PR threads (when JSON is requested):** only **high** severity findings may use
  file/line anchors. **Medium** and **low** findings must appear **only** in the PR-wide
  summary text, never as separate inline threads.

Constraints:
- You only see a git diff (possibly truncated). Do not invent files or behavior not shown.
- This review is NOT a substitute for SAST, secret scanning, or manual security review.
- Do not request or assume access to external systems beyond what the diff shows.
"""


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def main() -> None:
    load_dotenv()
    configure_ssl()
    endpoint = _require_env("PROJECT_ENDPOINT")
    model = _require_env("PR_RVW_MODEL_DEPLOYMENT_NAME")
    agent_name = os.environ.get("PR_RVW_AGENT_NAME", "pr-review-agent").strip()
    instructions = os.environ.get("AGENT_INSTRUCTIONS", DEFAULT_INSTRUCTIONS).strip()
    if not instructions:
        instructions = DEFAULT_INSTRUCTIONS

    project_client = AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
    )

    agent = project_client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(
            model=model,
            instructions=instructions,
        ),
    )
    print(
        f"Agent version created (id: {agent.id}, name: {agent.name}, version: {agent.version})"
    )


if __name__ == "__main__":
    main()
