"""GitHub client for fetching PRs and posting review comments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from github import Github
from github.PullRequest import PullRequest as GHPullRequest


@dataclass
class PRInfo:
    """Lightweight representation of a pull request."""

    number: int
    title: str
    author: str
    branch: str
    base: str
    url: str
    diff_url: str
    body: str
    changed_files: int


def parse_repo_url(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL.

    Accepts formats like:
        https://github.com/owner/repo
        https://github.com/owner/repo.git
        github.com/owner/repo
        owner/repo
    """
    url = url.strip().rstrip("/")

    # Already in owner/repo form (no scheme, no dots in first segment)
    if not url.startswith("http") and "/" in url and "." not in url.split("/")[0]:
        return url

    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse GitHub repo from URL: {url}")
    return f"{parts[0]}/{parts[1]}"


class GitHubClient:
    """Wraps PyGithub to fetch PRs and post reviews."""

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.gh = Github(self.token) if self.token else Github()

    def list_open_prs(self, repo_url: str) -> list[PRInfo]:
        """Return all open PRs for the given repo."""
        repo_slug = parse_repo_url(repo_url)
        repo = self.gh.get_repo(repo_slug)
        prs = repo.get_pulls(state="open", sort="updated", direction="desc")
        return [
            PRInfo(
                number=pr.number,
                title=pr.title,
                author=pr.user.login,
                branch=pr.head.ref,
                base=pr.base.ref,
                url=pr.html_url,
                diff_url=pr.diff_url,
                body=pr.body or "",
                changed_files=pr.changed_files,
            )
            for pr in prs
        ]

    def get_pr_diff(self, repo_url: str, pr_number: int) -> str:
        """Fetch the unified diff for a single PR."""
        repo_slug = parse_repo_url(repo_url)
        repo = self.gh.get_repo(repo_slug)
        pr = repo.get_pull(pr_number)
        files = pr.get_files()
        diff_parts: list[str] = []
        for f in files:
            diff_parts.append(f"--- a/{f.filename}\n+++ b/{f.filename}")
            if f.patch:
                diff_parts.append(f.patch)
        return "\n".join(diff_parts)

    def get_pr(self, repo_url: str, pr_number: int) -> GHPullRequest:
        """Get the raw PyGithub PullRequest object."""
        repo_slug = parse_repo_url(repo_url)
        repo = self.gh.get_repo(repo_slug)
        return repo.get_pull(pr_number)

    def post_review_comment(
        self, repo_url: str, pr_number: int, body: str
    ) -> None:
        """Post a general review comment on a PR."""
        pr = self.get_pr(repo_url, pr_number)
        pr.create_issue_comment(body)
