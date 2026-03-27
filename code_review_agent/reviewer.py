"""Core review agent that uses OpenHands to review PR diffs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.sdk.llm import Message, TextContent
from openhands.tools import TerminalTool, FileEditorTool

from .github_client import GitHubClient, PRInfo

REVIEW_SYSTEM_PROMPT = """\
You are an expert code reviewer. You will be given a pull request diff from a
GitHub repository. Analyze the changes and provide a thorough code review.

Focus on:
1. **Bugs & correctness** — logic errors, off-by-one, null/undefined issues
2. **Security** — injection, auth issues, secrets in code, OWASP top-10
3. **Performance** — unnecessary allocations, N+1 queries, missing indexes
4. **Readability** — unclear naming, missing context, overly complex logic
5. **Best practices** — error handling, testing gaps, API design

For each issue found, provide:
- The file and approximate line range
- Severity: critical / warning / suggestion
- A clear explanation of the problem
- A concrete fix or recommendation

If the PR looks good, say so — don't invent problems.
End with a brief summary and an overall assessment (approve / request-changes / comment).
"""

TRIAGE_PROMPT = """\
You are a PR triage assistant. Given a list of open pull requests, categorize
each by priority for review:
- HIGH: large diffs, security-sensitive paths, or core logic changes
- MEDIUM: moderate changes, feature additions
- LOW: docs, typos, config changes, dependency bumps

Return a ranked list with brief rationale for each.
"""


def _make_messages(system: str, user: str) -> list[Message]:
    """Build Message objects from system/user text."""
    return [
        Message(role="system", content=[TextContent(text=system)]),
        Message(role="user", content=[TextContent(text=user)]),
    ]


def _extract_text(response) -> str:
    """Extract text content from an LLMResponse."""
    if hasattr(response, "content") and isinstance(response.content, str):
        return response.content
    if hasattr(response, "content") and isinstance(response.content, list):
        return "".join(
            c.text for c in response.content if hasattr(c, "text")
        )
    if hasattr(response, "choices"):
        return response.choices[0].message.content
    return str(response)


@dataclass
class ReviewResult:
    """Result of reviewing a single PR."""

    pr: PRInfo
    review_text: str
    posted: bool = False


# Free models ordered by quality for code review.
# The agent tries each in order until one works.
FREE_MODEL_FALLBACK_CHAIN = [
    "gemini/gemini-2.5-flash",                          # 1M ctx, 500 req/day
    "openrouter/qwen/qwen3-coder:free",                 # 262K ctx, code-specific
    "groq/llama-3.3-70b-versatile",                     # 128K ctx, 1K req/day
    "cerebras/llama-3.3-70b",                            # 128K ctx, 30 RPM
    "openrouter/nousresearch/hermes-3-llama-3.1-405b:free",  # 131K ctx
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free", # 262K ctx
    "openrouter/meta-llama/llama-3.3-70b-instruct:free", # 128K ctx
]


@dataclass
class CodeReviewAgent:
    """Orchestrates PR discovery, triage, and review using OpenHands."""

    repo_url: str
    github_token: str | None = None
    llm_model: str = "gemini/gemini-2.5-flash"
    llm_api_key: str | None = None
    fallback: bool = True
    auto_post: bool = False
    _gh: GitHubClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._gh = GitHubClient(token=self.github_token)
        self.llm_api_key = self.llm_api_key or os.environ.get("LLM_API_KEY", "")

    def _make_llm(self, model: str | None = None) -> LLM:
        return LLM(model=model or self.llm_model, api_key=self.llm_api_key, temperature=0.0)

    def _call_llm_with_fallback(self, system: str, user: str) -> str:
        """Try the primary model, then fall back through free alternatives."""
        models = [self.llm_model]
        if self.fallback:
            models += [m for m in FREE_MODEL_FALLBACK_CHAIN if m != self.llm_model]

        messages = _make_messages(system, user)
        last_error = None
        for model in models:
            try:
                llm = self._make_llm(model)
                response = llm.completion(messages=messages)
                return _extract_text(response)
            except Exception as e:
                last_error = e
                continue

        raise RuntimeError(
            f"All models failed. Last error: {last_error}"
        )

    def list_prs(self) -> list[PRInfo]:
        """Fetch all open PRs."""
        return self._gh.list_open_prs(self.repo_url)

    def triage_prs(self, prs: list[PRInfo]) -> str:
        """Use LLM to triage/prioritize PRs."""
        pr_summary = "\n".join(
            f"- PR #{p.number}: {p.title} by @{p.author} "
            f"({p.changed_files} files changed) [{p.url}]"
            for p in prs
        )
        return self._call_llm_with_fallback(TRIAGE_PROMPT, pr_summary)

    def review_pr(self, pr_number: int) -> ReviewResult:
        """Review a single PR by number."""
        prs = self._gh.list_open_prs(self.repo_url)
        pr = next((p for p in prs if p.number == pr_number), None)
        if pr is None:
            raise ValueError(f"PR #{pr_number} not found or not open")

        diff = self._gh.get_pr_diff(self.repo_url, pr_number)

        prompt = (
            f"## PR #{pr.number}: {pr.title}\n"
            f"**Author:** @{pr.author}\n"
            f"**Branch:** {pr.branch} -> {pr.base}\n\n"
            f"### Description\n{pr.body}\n\n"
            f"### Diff\n```diff\n{diff}\n```"
        )
        review_text = self._call_llm_with_fallback(REVIEW_SYSTEM_PROMPT, prompt)

        posted = False
        if self.auto_post:
            self._gh.post_review_comment(
                self.repo_url, pr_number, f"## AI Code Review\n\n{review_text}"
            )
            posted = True

        return ReviewResult(pr=pr, review_text=review_text, posted=posted)

    def review_all_prs(self) -> list[ReviewResult]:
        """Review every open PR."""
        prs = self.list_prs()
        results = []
        for pr in prs:
            result = self.review_pr(pr.number)
            results.append(result)
        return results

    def review_with_openhands_agent(self, pr_number: int) -> str:
        """Use a full OpenHands agent session for deeper review.

        This clones the repo into a sandbox, checks out the PR branch,
        and lets the agent explore the code interactively.
        """
        llm = self._make_llm()
        agent = Agent(
            llm=llm,
            tools=[
                Tool(name=TerminalTool.name),
                Tool(name=FileEditorTool.name),
            ],
            system_message=REVIEW_SYSTEM_PROMPT,
        )
        conversation = Conversation(agent=agent)

        pr_info = next(
            (p for p in self.list_prs() if p.number == pr_number), None
        )
        if pr_info is None:
            raise ValueError(f"PR #{pr_number} not found or not open")

        message = (
            f"Clone the repository {self.repo_url} and check out PR #{pr_number} "
            f"(branch: {pr_info.branch}).\n"
            f"Run `git diff {pr_info.base}...{pr_info.branch}` to see the changes.\n"
            f"Then explore the changed files in context and provide a thorough code review.\n"
            f"Focus on bugs, security, performance, and readability."
        )
        conversation.send_message(message)
        conversation.run()
        return conversation.get_last_response()
