"use client";

import { useEffect, useState } from "react";

interface Agent {
  computer_id: string;
  short_id: string;
  repo: string;
  state: string;
}

interface Review {
  repo: string;
  title: string;
  number: number;
  url: string;
  updated: string;
}

interface StatusData {
  agents: Agent[];
  stats: {
    total: number;
    running: number;
    sleeping: number;
    failed: number;
    sleeping_pct: number;
    active_pct: number;
    samples: number;
  };
  usage: {
    runtime_gb_hours: number;
    cost_total: number;
    uptime_hours: number;
  };
  reviews: Review[];
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

  const { agents, stats, usage, reviews } = data;

  return (
    <div className="min-h-screen">
      {/* Header */}
      <header className="border-b border-[var(--border)] px-6 py-4">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-full bg-[var(--accent-rust)] flex items-center justify-center">
              <span className="text-white font-mono text-sm font-bold">O</span>
            </div>
            <span className="font-serif text-xl text-[var(--text-primary)]">
              Orb Code Reviewer
            </span>
          </div>
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
            label="Computers"
            value={stats.total}
            sub={`${stats.running} active, ${stats.sleeping} sleeping`}
          />
          <StatCard
            label="Active"
            value={`${stats.active_pct}%`}
            sub="of total time"
          />
          <StatCard
            label="Sleeping"
            value={`${stats.sleeping_pct}%`}
            sub="zero cost"
          />
          <StatCard
            label="Cost"
            value={`$${usage.cost_total.toFixed(2)}`}
            sub={`${usage.uptime_hours}h uptime`}
          />
          <StatCard
            label="Reviews"
            value={reviews.length}
            sub="PRs reviewed"
          />
        </div>

        {/* Sleeping percentage bar */}
        <div className="mb-10 border border-[var(--border)] rounded-xl p-5 bg-[var(--bg-card)]">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs uppercase tracking-widest text-[var(--text-tertiary)] font-mono">
              Time allocation since start
            </span>
            <span className="text-xs text-[var(--text-secondary)] font-mono">
              {stats.samples} samples
            </span>
          </div>
          <div className="flex h-6 rounded-full overflow-hidden bg-[var(--bg-darkest)]">
            <div
              className="bg-[var(--accent-green)] transition-all duration-500"
              style={{ width: `${stats.active_pct}%` }}
            />
            <div
              className="bg-[var(--text-tertiary)] transition-all duration-500"
              style={{ width: `${stats.sleeping_pct}%` }}
            />
          </div>
          <div className="flex justify-between mt-2 text-xs font-mono">
            <span className="text-[var(--accent-green)]">
              Active {stats.active_pct}%
            </span>
            <span className="text-[var(--text-tertiary)]">
              Sleeping {stats.sleeping_pct}% (free)
            </span>
          </div>
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
                      <a
                        href={`https://github.com/${agent.repo}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-mono text-sm text-[var(--text-primary)] hover:text-[var(--accent-rust)] transition-colors"
                      >
                        {agent.repo}
                      </a>
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
                  <div className="mt-2 text-xs text-[var(--text-tertiary)] font-mono">
                    {agent.short_id}
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
                        #{review.number}
                      </span>
                      <span className="text-sm text-[var(--text-primary)] truncate max-w-md">
                        {review.title}
                      </span>
                    </div>
                    <span className="text-xs text-[var(--text-tertiary)] font-mono whitespace-nowrap ml-4">
                      {timeAgo(review.updated)}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-[var(--text-secondary)] font-mono">
                    {review.repo}
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
