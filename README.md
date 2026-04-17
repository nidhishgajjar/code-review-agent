# code-review-agent

An autonomous AI reviewer for GitHub pull requests. Runs as a webhook-driven
sandbox on [Orb Cloud](https://docs.orbcloud.dev). Uses the official
[OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)
as its reasoning engine. Asleep by default, costs nothing between reviews.

Live dashboard: https://code-review-dashboard-seven.vercel.app

## What it does

1. GitHub fires a `pull_request` webhook at the agent's public Orb URL
2. The agent wakes up in under a second, verifies the HMAC signature, and
   kicks off a background review thread
3. It `git fetch`es (or clones) the repo into its persistent volume, fetches
   the PR diff, and hands both to an OpenHands agent (terminal + file editor
   tools, bounded to the cloned directory)
4. OpenHands explores the codebase on its own, writes the final review to
   `REVIEW.md`, and the wrapper posts that as a PR comment
5. Records the PR in a local state file so redeliveries don't double-post
6. Goes back to sleep — Orb checkpoints the sandbox to NVMe with no running
   cost until the next webhook

On startup, the agent reads `repos.txt` and auto-registers a webhook on each
listed repository pointing at its own public URL (idempotent — skips repos
that already have the hook).

## Architecture

```
              GitHub                                    Orb Cloud sandbox
   ┌──────────────────────────┐              ┌──────────────────────────────┐
   │  pull_request webhook    │───POST──────▶│  Flask webhook server        │
   │  signed with HMAC-SHA256 │              │    (agent.py, waitress)      │
   └──────────────────────────┘              │           │                  │
                                             │           │ verify HMAC,     │
                                             │           │ dedup, ack 202   │
                                             │           ▼                  │
                                             │  background review thread    │
                                             │           │                  │
                                             │           ▼                  │
                                             │  OpenHands SDK               │
                                             │    ├─ TerminalTool           │
                                             │    └─ FileEditorTool         │
                                             │           │                  │
                                             │           ▼                  │
                                             │  LiteLLM ──▶ Anthropic /     │
                                             │              OpenRouter /    │
                                             │              z.ai GLM / ...  │
                                             │           │                  │
   ◀──── POST /issues/n/comments ────────────┼───────────┘                  │
                                             │                              │
                                             │  sandbox freezes when idle;  │
                                             │  webhook wakes it in ~1 s    │
                                             └──────────────────────────────┘
```

There is no polling and no custom agent loop. All reasoning runs through
OpenHands. `agent.py` is a thin harness that owns the HTTP plumbing, dedup,
and GitHub I/O. OpenHands owns the review itself.

## Review format

Each posted review uses this structure:

- **Summary** — what the PR does, in project context
- **Architecture** — how it fits, referencing specific files read
- **Issues** — `[severity] path:line — problem — concrete fix` (critical / warning / suggestion)
- **Cross-file impact** — what this could break elsewhere
- **Assessment** — approve / request-changes / comment

## Deduplication

A PR is skipped if either is true:

- Its `{owner/repo}#{number}` is already in `state.json` at `/agent/data/state/agent.json`
- There is already a comment from our GitHub login on that PR

The second check is the belt to the state file's suspenders — it prevents
duplicate posts even if state is lost (Orb sandbox recreate, disk wipe, etc.).

A `REVIEW_LOCK` threading lock serialises concurrent webhook-triggered
reviews so two deliveries for the same PR can't race.

## Files in this repo

- `agent.py` — Flask webhook server, HMAC verification, dedup, OpenHands handoff, hook bootstrap
- `runner.py` — process supervisor; restarts `agent.py` on crash, tees logs to `/agent/data/logs/`
- `repos.txt` — the allowlist of `owner/name` repos this agent is responsible for
- `orb.toml` — Orb Cloud deployment config (runtime, env vars, build steps, LLM provider, exposed port, idle timeout)
- `requirements.txt` — Python deps: `requests`, `flask`, `waitress`, `openhands-sdk`, `openhands-tools`

## Configuration

All runtime config is env vars (set from `[agent.env]` in `orb.toml`, with
`${SECRET}` references resolved at deploy time from `org_secrets`):

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | GitHub PAT with `public_repo` scope (read PRs, post comments, register webhooks) |
| `GITHUB_WEBHOOK_SECRET` | Random 32-byte hex. Used by GitHub to sign webhook payloads; agent verifies HMAC-SHA256 against it |
| `PUBLIC_URL` | The agent's public Orb URL, e.g. `https://{id8}.orbcloud.dev`. Bootstrap registers the hook against `$PUBLIC_URL/webhook` |
| `LLM_API_KEY` | API key for the LLM provider |
| `LLM_BASE_URL` | Provider base URL (e.g. `https://api.z.ai/api/anthropic`, `https://openrouter.ai/api/v1`) |
| `LLM_MODEL` | LiteLLM-style model id (e.g. `anthropic/glm-4.6`, `openrouter/anthropic/claude-sonnet-4-5`) |
| `PORT` | Port the Flask server listens on. Default `8080`; must match `[ports] expose` in `orb.toml` |
| `STATE_DIR` | Where to persist dedup state. On Orb: `/agent/data/state` |
| `WORKDIR` | Where to clone repos. On Orb: `/agent/data/work` |
| `REPOS_FILE` | Path to the repo allowlist. Default `./repos.txt` |

The LLM is called via LiteLLM inside OpenHands, so any provider LiteLLM
supports works (Anthropic, OpenAI, OpenRouter, Google, z.ai, Groq, Together,
DeepSeek, self-hosted, etc.).

## Deploying to Orb Cloud

```bash
ORB=orb_...  # your Orb API key

# 1. Create a computer (4 GB RAM, 6 GB disk is a good baseline)
RESP=$(curl -s -X POST https://api.orbcloud.dev/v1/computers \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/json' \
  -d '{"name":"code-review-agent","runtime_mb":4096,"disk_mb":6144}')
CID=$(echo "$RESP" | jq -r .id)
PUBLIC="https://$(echo $CID | cut -c1-8).orbcloud.dev"

# 2. Upload this repo's orb.toml as the config
curl -s -X POST "https://api.orbcloud.dev/v1/computers/$CID/config" \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/toml' \
  --data-binary @orb.toml

# 3. Build (clones this repo, installs deps — takes 2-3 min with openhands)
curl -s -X POST "https://api.orbcloud.dev/v1/computers/$CID/build" \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/json' \
  -d '{"org_secrets":{"GITHUB_TOKEN":"ghp_..."}}'

# 4. Generate a webhook secret and start the agent
SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
curl -s -X POST "https://api.orbcloud.dev/v1/computers/$CID/agents" \
  -H "Authorization: Bearer $ORB" -H 'Content-Type: application/json' \
  -d "{\"task\":\"start\",\"org_secrets\":{
        \"GITHUB_TOKEN\":\"ghp_...\",
        \"GLM_API_KEY\":\"...\",
        \"GITHUB_WEBHOOK_SECRET\":\"$SECRET\",
        \"PUBLIC_URL\":\"$PUBLIC\"}}"
```

On startup the agent auto-registers a webhook on each repo listed in
`repos.txt`, pointing at `$PUBLIC_URL/webhook`. Swapping in new repos is a
matter of editing `repos.txt`, pushing, and rebuilding — the next startup
picks up the list.

The agent exposes two HTTP endpoints:

- `POST /webhook` — GitHub delivers `pull_request` events here
- `GET /health` — returns `{ok, allowlist}`; used by the dashboard and smoke tests

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

export GITHUB_TOKEN=ghp_...
export GITHUB_WEBHOOK_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
export PUBLIC_URL=http://localhost:8080   # skip webhook bootstrap with "" to keep it local-only
export LLM_API_KEY=...
export LLM_BASE_URL=https://api.z.ai/api/anthropic
export LLM_MODEL=anthropic/glm-4.6
export REPOS_FILE=./repos.txt

.venv/bin/python -u agent.py
```

To drive a test review locally, compute the HMAC of a saved GitHub
`pull_request` payload and POST it to `http://localhost:8080/webhook` with
the `X-Hub-Signature-256` and `X-GitHub-Event: pull_request` headers.

## Scaling to multiple agents

Each Orb computer is one agent. To scale to N agents, deploy N separate Orb
computers, each with its own `repos.txt` and its own `PUBLIC_URL`. GitHub
routes each repo's webhook directly to the agent that owns it — no central
dispatcher needed when each agent is pre-assigned a disjoint repo set.

## License

MIT
