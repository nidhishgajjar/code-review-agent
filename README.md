# AI Code Reviewer

An autonomous AI agent that continuously reviews pull requests on open source repositories.

## What it does

1. Claims open source repositories from a pool
2. Clones each repo, understands the codebase structure
3. Monitors for open pull requests
4. Reviews each PR with full context — reads the diff, explores surrounding code, checks cross-file impact
5. Posts a detailed review comment on the PR
6. Moves on to the next repo, claims more when idle
7. Runs forever

## How it works

The agent uses [OpenHands](https://github.com/All-Hands-AI/OpenHands) in headless mode. Each cycle:

- Call a claim API to get assigned repositories
- For each repo: `git clone`, check open PRs, fetch diffs
- For unreviewed PRs: read the diff + surrounding code, analyze for bugs, security, performance, architecture
- Post a review comment via GitHub API
- Report back to the claim API
- Sleep 30 seconds, repeat

The agent maintains state across cycles via session resume (`--resume`). It remembers which PRs it already reviewed and which repos it monitors.

## Review format

Each review comment includes:

- **Summary** — what the PR does
- **Architecture** — how it fits the codebase
- **Issues** — file, severity (critical/warning/suggestion), explanation, fix
- **Cross-file impact** — anything in other files affected
- **Assessment** — approve / request-changes / comment

## Tech stack

- **Agent runtime:** [OpenHands](https://github.com/All-Hands-AI/OpenHands) CLI (headless mode)
- **LLM:** Any OpenAI-compatible or Anthropic-compatible model (configurable)
- **GitHub API:** For reading PRs and posting comments
- **Claim API:** Central coordination so multiple agents don't review the same repos

## Requirements

- OpenHands CLI installed
- GitHub PAT with `public_repo` scope
- LLM API key (OpenRouter, Anthropic, OpenAI, etc.)
- Python 3.12+

## Configuration

Set these environment variables:

```bash
GITHUB_TOKEN=ghp_...          # GitHub PAT for reading PRs and posting comments
LLM_MODEL=anthropic/claude-sonnet-4-20250514   # or any model
LLM_API_KEY=sk-...            # your LLM provider API key
LLM_BASE_URL=https://api.anthropic.com          # your LLM provider endpoint
```

## Running

```bash
openhands --headless -t "You are a code review agent. Check for open PRs on the repos assigned to you, review them, post comments. Never stop."
```

## License

MIT
