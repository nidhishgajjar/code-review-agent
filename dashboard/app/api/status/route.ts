import { NextResponse } from "next/server";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { join } from "path";

const ORB_API = "https://api.orbcloud.dev/v1";
const ORB_KEY = process.env.ORB_API_KEY!;
const GITHUB_TOKEN = process.env.GITHUB_TOKEN!;

const DATA_DIR = "/opt/review-dashboard/data";
const STATS_FILE = join(DATA_DIR, "stats.json");
const REPOS_FILE = join(DATA_DIR, "repos.json");

interface Stats {
  total_samples: number;
  sleeping_samples: number;
  running_samples: number;
  total_reviews: number;
  started_at: string;
}

function loadStats(): Stats {
  try {
    if (existsSync(STATS_FILE)) {
      return JSON.parse(readFileSync(STATS_FILE, "utf-8"));
    }
  } catch {}
  return {
    total_samples: 0,
    sleeping_samples: 0,
    running_samples: 0,
    total_reviews: 0,
    started_at: new Date().toISOString(),
  };
}

function saveStats(stats: Stats) {
  try {
    if (!existsSync(DATA_DIR)) mkdirSync(DATA_DIR, { recursive: true });
    writeFileSync(STATS_FILE, JSON.stringify(stats, null, 2));
  } catch {}
}

function loadRepoAssignments(): Record<string, string[]> {
  try {
    if (existsSync(REPOS_FILE)) {
      const data = JSON.parse(readFileSync(REPOS_FILE, "utf-8"));
      const byAgent: Record<string, string[]> = {};
      for (const r of data.repos || []) {
        if (r.assigned_to) {
          if (!byAgent[r.assigned_to]) byAgent[r.assigned_to] = [];
          byAgent[r.assigned_to].push(r.repo);
        }
      }
      return byAgent;
    }
  } catch {}
  return {};
}

async function orbFetch(path: string) {
  const res = await fetch(`${ORB_API}${path}`, {
    headers: { Authorization: `Bearer ${ORB_KEY}` },
    next: { revalidate: 0 },
  });
  return res.json();
}

interface ReviewEntry {
  repo: string;
  number: string;
  url: string;
  agent: string;
}

async function fetchReviewData(): Promise<{ total: number; items: ReviewEntry[] }> {
  let total = 0;
  const allItems: ReviewEntry[] = [];

  try {
    const computersData = await orbFetch("/computers");
    for (const c of computersData.computers || []) {
      try {
        const res = await fetch(
          `${ORB_API}/computers/${c.id}/files/root/data/reviewed_prs.txt`,
          { headers: { Authorization: `Bearer ${ORB_KEY}` }, next: { revalidate: 0 } }
        );
        if (res.ok) {
          const text = await res.text();
          const lines = text.trim().split("\n").filter((l: string) => l.trim());
          total += lines.length;
          for (const line of lines.slice(-10)) {
            const parts = line.trim().split(" ");
            if (parts.length >= 2) {
              const repo = parts[0];
              const prNum = parts[1].replace("PR", "");
              allItems.push({
                repo,
                number: prNum,
                url: `https://github.com/${repo}/pull/${prNum}`,
                agent: c.name,
              });
            }
          }
        }
      } catch {}
    }
  } catch {}

  // Sort by PR number descending (rough proxy for recency), take last 20
  const recent = allItems.slice(-20).reverse();
  return { total, items: recent };
}

export async function GET() {
  try {
    // Get all computers from Orb API
    const computersData = await orbFetch("/computers");
    const computers = computersData.computers || [];

    // Get repo assignments
    const assignments = loadRepoAssignments();

    const agents: Array<{
      computer_id: string;
      short_id: string;
      name: string;
      state: string;
      repos: string[];
    }> = [];

    let running = 0;
    let sleeping = 0;
    let failed = 0;

    for (const c of computers) {
      try {
        const agentData = await orbFetch(`/computers/${c.id}/agents`);
        const agentList = agentData.agents || [];
        const active =
          agentList.find((a: any) => a.state !== "failed") || agentList[0];
        const state = active?.state || "unknown";

        if (state === "running") running++;
        else if (state === "checkpointed") sleeping++;
        else if (state === "failed") failed++;

        agents.push({
          computer_id: c.id,
          short_id: c.id.slice(0, 8),
          name: c.name,
          state,
          repos: assignments[c.id] || [],
        });
      } catch {
        agents.push({
          computer_id: c.id,
          short_id: c.id.slice(0, 8),
          name: c.name,
          state: "unknown",
          repos: [],
        });
      }
    }

    // Usage
    const usage = await orbFetch("/usage");
    const runtimeGbHours = usage.runtime_gb_hours || 0;
    const diskGbHours = usage.disk_gb_hours || 0;
    const costRuntime = runtimeGbHours * 0.005;
    const costDisk = (diskGbHours / 720) * 0.05;
    const totalCost = costRuntime + costDisk;

    // Stats
    const stats = loadStats();
    const uptimeMs = Date.now() - new Date(stats.started_at).getTime();
    const uptimeHours = Math.round((uptimeMs / 3600000) * 10) / 10;

    // Reviews
    const reviewData = await fetchReviewData();

    // Repo pool stats
    let totalRepos = 0;
    let claimedRepos = 0;
    try {
      if (existsSync(REPOS_FILE)) {
        const repoData = JSON.parse(readFileSync(REPOS_FILE, "utf-8"));
        totalRepos = (repoData.repos || []).length;
        claimedRepos = (repoData.repos || []).filter((r: any) => r.assigned_to).length;
      }
    } catch {}

    return NextResponse.json({
      agents,
      stats: {
        total_agents: agents.length,
        running,
        sleeping,
        failed,
        total_repos: totalRepos,
        claimed_repos: claimedRepos,
      },
      usage: {
        runtime_gb_hours: Math.round(runtimeGbHours * 100) / 100,
        cost_total: Math.round(totalCost * 100) / 100,
        uptime_hours: uptimeHours,
      },
      reviews: reviewData.items,
      total_reviews: reviewData.total,
      started_at: stats.started_at,
      timestamp: new Date().toISOString(),
    });
  } catch (error: any) {
    return NextResponse.json(
      { error: error.message || "Failed to fetch status" },
      { status: 500 }
    );
  }
}
