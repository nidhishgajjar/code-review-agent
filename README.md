# code-review-agent

An autonomous AI agent that continuously reviews pull requests on a pool of
GitHub repositories. Runs as a long-lived process on [Orb Cloud](https://docs.orbcloud.dev).
Uses the official [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)
as its reasoning engine.

## What it does

1. Reads a list of repositories from `repos.txt`
2. For each repo, clones it and lists open PRs via the GitHub API
3. For any PR it has not already reviewed:
   - Hands the diff + the cloned workspace to an OpenHands agent
   - The agent explores the repo (bash + file-editor tools), forms an opinion,
     and writes a review to `REVIEW.md`
   - Posts that review as a PR comment
4. Records the PR in a local state file, skips it on future cycles
5. Sleeps `POLL_INTERVAL` seconds, repeats forever

## Architecture

```
                                 Orb Cloud sandbox
                    ┌──────────────────────────────────────────┐
                    │  runner.py  (supervises workers)         │
                    │      │                                   │
                    │      ▼                                   │
                    │  agent.py  (poll loop)                   │
                    │      │                                   │
    GitHub API  ◀──▶│      ├─ git fetch / list PRs / post  ─▶ GitHub
                    │      │                                   │
                    │      ▼                                   │
                    │  OpenHands SDK  (Agent + Conversation)   │
                    │   ├─ TerminalTool                        │
                    │   └─ FileEditorTool                      │
                    │      │                                   │
                    │      ▼                                   │
    LLM provider ◀──│  LiteLLM  ──▶  Anthropic / OpenRouter /  │
                    │                z.ai Coding Plan / ...    │
                    └──────────────────────────────────────────┘
```

There is no custom agent loop. All exploration, tool-use, and reasoning run
through OpenHands. `agent.py` is a thin harness: it decides which PRs to review
and owns the GitHub I/O; OpenHands owns the review itself.

## Review format

Each posted review uses this structure:

- **Summary** — what the PR does, in project context
- **Architecture** — how it fits, referencing specific files read
- **Issues** — `[severity] path:line — problem — concrete fix` (critical / warning / suggestion)
- **Cross-file impact** — what this could break elsewhere
- **Assessment** — approve / request-changes / comment

## Deduplication

A PR is skipped on future cycles if either is true:
- Its `{owner/repo}#{number}` is in the local state file
  (`$STATE_DIR/agent-{id}.json`, persisted to `/agent/data/state` on Orb)
- There is already a comment from our GitHub login on that PR

The second check is the belt to the state-file's suspenders — it prevents
duplicate posts even if the state file is lost.

## Files in this repo

- `agent.py` — poll loop, GitHub I/O, dedup, hands reviews to OpenHands
- `runner.py` — process supervisor; respawns crashed workers, tees logs to `/agent/data/logs/`
- `repos.txt` — the pool of `owner/name` repos to review (one per line, `#` for comments)
- `orb.toml` — Orb Cloud deployment config (runtime, env vars, build steps, LLM provider, idle timeout)
- `requirements.txt` — Python deps: `requests`, `openhands-sdk`, `openhands-tools`

## Configuration

All runtime config is env vars (set from `[agent.env]` in `orb.toml`, with
`${SECRET}` references resolved at deploy time from `org_secrets`):

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | GitHub PAT with `public_repo` scope (read PRs, post comments) |
| `LLM_API_KEY` | API key for the LLM provider |
| `LLM_BASE_URL` | Provider base URL (e.g. `https://api.z.ai/api/anthropic`, `https://openrouter.ai/api/v1`) |
| `LLM_MODEL` | LiteLLM-style model id (e.g. `anthropic/glm-4.6`, `openrouter/anthropic/claude-sonnet-4-5`) |
| `NUM_AGENTS` | Number of parallel worker processes `runner.py` should spawn. Default 1 |
| `POLL_INTERVAL` | Seconds between GitHub poll cycles. Default 60 |
| `STATE_DIR` | Where to persist dedup state. On Orb: `/agent/data/state` |
| `WORKDIR` | Where to clone repos. On Orb: `/agent/data/work` |
| `REPOS_FILE` | Path to the repo pool list. Default `./repos.txt` |

The LLM is called via LiteLLM inside OpenHands, so any provider LiteLLM
supports works (Anthropic, OpenAI, OpenRouter, Google, z.ai, Groq, Together,
DeepSeek, self-hosted, etc.).

## Deploying to Orb Cloud

```bash
ORB=orb_...  # your Orb API key

# 1. Create a computer
curl -sX POST https://api.orbcloud.dev/v1/computers \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/json' \
  -d '{"name":"code-review-agent","runtime_mb":4096,"disk_mb":6144}'
# save the returned {id} as CID

# 2. Upload this repo's orb.toml as the config
curl -sX POST "https://api.orbcloud.dev/v1/computers/$CID/config" \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/toml' \
  --data-binary @orb.toml

# 3. Build (clones this repo, installs deps)
curl -sX POST "https://api.orbcloud.dev/v1/computers/$CID/build" \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/json' \
  -d '{"org_secrets":{"GITHUB_TOKEN":"ghp_..."}}'

# 4. Start the agent
curl -sX POST "https://api.orbcloud.dev/v1/computers/$CID/agents" \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/json' \
  -d '{"task":"start","org_secrets":{"GITHUB_TOKEN":"ghp_...","GLM_API_KEY":"..."}}'
```

To swap which repos are being reviewed: edit `repos.txt`, push, then trigger a
rebuild (POST `/build`). The next cycle picks up the new list.

## Running locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

export GITHUB_TOKEN=ghp_...
export LLM_API_KEY=...
export LLM_BASE_URL=https://api.z.ai/api/anthropic
export LLM_MODEL=anthropic/glm-4.6
export ONE_SHOT=1              # run one cycle and exit (else loop forever)
export REPOS_FILE=./repos.txt

.venv/bin/python -u agent.py
```

`ONE_SHOT=1` is useful for testing a single review end-to-end. Without it,
`agent.py` runs forever.

## License

MIT
