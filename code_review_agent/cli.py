"""CLI interface for the code review agent."""

from __future__ import annotations

import os
import sys

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .reviewer import CodeReviewAgent

console = Console()


@click.group()
@click.option(
    "--repo",
    required=True,
    envvar="REVIEW_REPO_URL",
    help="GitHub repo URL (e.g. https://github.com/owner/repo)",
)
@click.option(
    "--model",
    default="gemini/gemini-2.5-flash",
    envvar="LLM_MODEL",
    help="LLM model to use (LiteLLM format). Default: gemini/gemini-2.5-flash (free)",
)
@click.option(
    "--no-fallback",
    is_flag=True,
    default=False,
    help="Disable automatic fallback to other free models on failure",
)
@click.option(
    "--auto-post/--no-auto-post",
    default=False,
    help="Automatically post reviews as PR comments",
)
@click.pass_context
def main(ctx: click.Context, repo: str, model: str, no_fallback: bool, auto_post: bool) -> None:
    """AI-powered code review agent built on OpenHands.

    Uses free LLM providers by default (Gemini, OpenRouter, Groq, Cerebras).
    Set the appropriate API key env var for your chosen provider.
    """
    ctx.ensure_object(dict)
    ctx.obj["agent"] = CodeReviewAgent(
        repo_url=repo,
        llm_model=model,
        fallback=not no_fallback,
        auto_post=auto_post,
    )


@main.command()
@click.pass_context
def list_prs(ctx: click.Context) -> None:
    """List all open pull requests."""
    agent: CodeReviewAgent = ctx.obj["agent"]

    with console.status("Fetching open PRs..."):
        prs = agent.list_prs()

    if not prs:
        console.print("[yellow]No open PRs found.[/yellow]")
        return

    table = Table(title=f"Open PRs — {agent.repo_url}")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Title", style="white")
    table.add_column("Author", style="green")
    table.add_column("Branch", style="magenta")
    table.add_column("Files", justify="right")

    for pr in prs:
        table.add_row(
            str(pr.number), pr.title, f"@{pr.author}", pr.branch, str(pr.changed_files)
        )

    console.print(table)


@main.command()
@click.pass_context
def triage(ctx: click.Context) -> None:
    """Triage and prioritize open PRs using LLM."""
    agent: CodeReviewAgent = ctx.obj["agent"]

    with console.status("Fetching PRs..."):
        prs = agent.list_prs()

    if not prs:
        console.print("[yellow]No open PRs found.[/yellow]")
        return

    with console.status("Triaging PRs with LLM..."):
        result = agent.triage_prs(prs)

    console.print(Markdown(result))


@main.command()
@click.argument("pr_number", type=int)
@click.option("--deep", is_flag=True, help="Use full OpenHands agent for deeper review")
@click.pass_context
def review(ctx: click.Context, pr_number: int, deep: bool) -> None:
    """Review a specific PR by number."""
    agent: CodeReviewAgent = ctx.obj["agent"]

    with console.status(f"Reviewing PR #{pr_number}..."):
        if deep:
            review_text = agent.review_with_openhands_agent(pr_number)
            console.print(Markdown(review_text))
        else:
            result = agent.review_pr(pr_number)
            console.print(Markdown(result.review_text))
            if result.posted:
                console.print("[green]Review posted to GitHub.[/green]")


@main.command()
@click.pass_context
def review_all(ctx: click.Context) -> None:
    """Review all open PRs."""
    agent: CodeReviewAgent = ctx.obj["agent"]

    with console.status("Fetching PRs..."):
        prs = agent.list_prs()

    if not prs:
        console.print("[yellow]No open PRs found.[/yellow]")
        return

    console.print(f"Found {len(prs)} open PRs. Reviewing...")

    for pr in prs:
        console.rule(f"PR #{pr.number}: {pr.title}")
        with console.status(f"Reviewing PR #{pr.number}..."):
            result = agent.review_pr(pr.number)
        console.print(Markdown(result.review_text))
        if result.posted:
            console.print("[green]Review posted to GitHub.[/green]")
        console.print()


if __name__ == "__main__":
    main()
