"use client";

import { useEffect, useState } from "react";

interface Agent {
  computer_id: string;
  short_id: string;
  name: string;
  state: string;
  repos: string[];
}

interface Review {
  repo: string;
  number: string;
  url: string;
  agent: string;
}

interface StatusData {
  agents: Agent[];
  stats: {
    total_agents: number;
    running: number;
    sleeping: number;
    failed: number;
    total_repos: number;
    claimed_repos: number;
  };
  usage: {
    runtime_gb_hours: number;
    cost_total: number;
    uptime_hours: number;
  };
  reviews: Review[];
  total_reviews: number;
  started_at: string;
  timestamp: string;
}

function StatusDot({ state }: { state: string }) {
  const cls =
    state === "running"
      ? "status-running"
      : state === "checkpointed"
      ? "status-sleeping"
      : state === "failed"
      ? "status-failed"
      : "bg-[var(--text-tertiary)]";
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${cls}`} />;
}

function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: string | number;
  sub?: string;
}) {
  return (
    <div className="border border-[var(--border)] rounded-xl p-5 bg-[var(--bg-card)]">
      <div className="text-xs uppercase tracking-widest text-[var(--text-tertiary)] font-mono mb-2">
        {label}
      </div>
      <div className="text-3xl font-mono font-bold text-[var(--text-primary)]">
        {value}
      </div>
      {sub && (
        <div className="text-sm text-[var(--text-secondary)] mt-1">{sub}</div>
      )}
    </div>
  );
}

function stateLabel(state: string) {
  if (state === "running") return "REVIEWING";
  if (state === "checkpointed") return "SLEEPING";
  if (state === "failed") return "FAILED";
  return state.toUpperCase();
}

function timeAgo(dateStr: string) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function Home() {
  const [data, setData] = useState<StatusData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetch("/api/status");
        if (!res.ok) throw new Error("Failed to fetch");
        setData(await res.json());
        setError(null);
      } catch (e: any) {
        setError(e.message);
      }
    };

    fetchStatus();
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, []);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-[var(--accent-rose)] font-mono">Error: {error}</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-[var(--text-secondary)] font-mono animate-pulse">
          Loading...
        </p>
      </div>
    );
  }

  const { agents, stats, usage, reviews, total_reviews } = data;

  return (
    <div className="min-h-screen">
      {/* Header */}
      <header className="border-b border-[var(--border)] px-6 py-4">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <span className="font-serif text-xl text-[var(--text-primary)]">
            Orb Code Reviewer
          </span>
          <a
            href="https://orbcloud.dev"
            className="text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors font-mono"
          >
            orbcloud.dev
          </a>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-10">
        {/* Hero stats */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-10">
          <StatCard
            label="Agents"
            value={stats.total_agents}
            sub={`${stats.running} active, ${stats.sleeping} sleeping`}
          />
          <StatCard
            label="Repos"
            value={stats.claimed_repos}
            sub={`of ${stats.total_repos} in pool`}
          />
          <StatCard
            label="Reviews"
            value={total_reviews}
            sub="PRs reviewed"
          />
          <StatCard
            label="Cost"
            value={`$${usage.cost_total.toFixed(2)}`}
            sub={`${usage.uptime_hours}h uptime`}
          />
          <StatCard
            label="Runtime"
            value={`${usage.runtime_gb_hours}`}
            sub="GB-hours"
          />
        </div>

        {/* Agent grid */}
        <div className="mb-10">
          <h2 className="text-xs uppercase tracking-widest text-[var(--text-tertiary)] font-mono mb-4">
            Agents
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {agents
              .sort((a, b) => {
                const order: Record<string, number> = {
                  running: 0,
                  checkpointed: 1,
                  failed: 2,
                };
                return (order[a.state] ?? 3) - (order[b.state] ?? 3);
              })
              .map((agent) => (
                <div
                  key={agent.computer_id}
                  className={`border border-[var(--border)] rounded-lg p-4 transition-all duration-200 ${
                    agent.state === "running"
                      ? "bg-[var(--bg-card-hover)] border-[var(--accent-green)]/20"
                      : "bg-[var(--bg-card)]"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <StatusDot state={agent.state} />
                      <span className="font-mono text-sm text-[var(--text-primary)]">
                        {agent.name}
                      </span>
                    </div>
                    <span
                      className={`text-xs font-mono uppercase tracking-wider ${
                        agent.state === "running"
                          ? "text-[var(--accent-green)]"
                          : agent.state === "checkpointed"
                          ? "text-[var(--text-tertiary)]"
                          : "text-[var(--accent-rose)]"
                      }`}
                    >
                      {stateLabel(agent.state)}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {agent.repos.length > 0 ? agent.repos.map((repo, i) => (
                      <a
                        key={i}
                        href={`https://github.com/${repo}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs font-mono text-[var(--text-secondary)] hover:text-[var(--accent-rust)] transition-colors"
                      >
                        {repo}
                      </a>
                    )) : (
                      <span className="text-xs text-[var(--text-tertiary)] font-mono">
                        claiming first repo...
                      </span>
                    )}
                  </div>
                </div>
              ))}
          </div>
        </div>

        {/* Recent reviews feed */}
        <div className="mb-10">
          <h2 className="text-xs uppercase tracking-widest text-[var(--text-tertiary)] font-mono mb-4">
            Recent Reviews
          </h2>
          {reviews.length === 0 ? (
            <p className="text-[var(--text-secondary)] text-sm">
              No reviews posted yet. Agents are warming up...
            </p>
          ) : (
            <div className="space-y-2">
              {reviews.map((review, i) => (
                <a
                  key={i}
                  href={review.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block border border-[var(--border)] rounded-lg p-4 bg-[var(--bg-card)] hover:bg-[var(--bg-card-hover)] transition-colors animate-fade-in"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-[var(--accent-rust)] font-mono text-sm">
                        PR #{review.number}
                      </span>
                      <span className="text-sm text-[var(--text-primary)] truncate max-w-md">
                        {review.repo}
                      </span>
                    </div>
                    <span className="text-xs text-[var(--text-tertiary)] font-mono whitespace-nowrap ml-4">
                      {review.agent}
                    </span>
                  </div>
                </a>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <footer className="border-t border-[var(--border)] pt-6 pb-10 text-center">
          <p className="text-sm text-[var(--text-tertiary)]">
            Powered by{" "}
            <a
              href="https://orbcloud.dev"
              className="text-[var(--accent-rust)] hover:underline"
            >
              Orb Cloud
            </a>{" "}
            - AI agents that sleep when idle, wake on demand.
          </p>
          <p className="text-xs text-[var(--text-tertiary)] mt-2 font-mono">
            Runtime: $0.005/GB-hr | Sleeping: $0 | LLM: GLM 5.1
          </p>
        </footer>
      </main>
    </div>
  );
}
