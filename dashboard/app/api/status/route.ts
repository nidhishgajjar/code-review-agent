import { NextResponse } from "next/server";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { join } from "path";

const ORB_API = "https://api.orbcloud.dev/v1";
const ORB_KEY = process.env.ORB_API_KEY!;
const GITHUB_TOKEN = process.env.GITHUB_TOKEN!;

// The 10 deployed computers (review@orbcloud.dev org)
const COMPUTER_REPOS: Record<string, string> = {
  "73875f64-7256-42a0-a16e-e67ae237a70c": "microsoft/autogen",
  "dfb5e3e2-73ee-49f9-a684-0a7a439b0820": "huggingface/transformers",
  "50b8b176-1cbd-4bcc-b753-40ca45b7c744": "anthropics/anthropic-cookbook",
  "ea48df3f-d68c-44af-aeac-4bd3eb886e51": "fastapi/fastapi",
  "d4aa5bf0-4d71-42f2-9ca6-e953f21d2c35": "nodejs/node",
  "81b29ba1-de87-42c4-b71a-9689c95f7139": "facebook/react",
  "e1c9fdbc-aaf2-4281-92ef-75ada953d3a6": "vercel/next.js",
  "f8b3fd17-0748-480b-8b5e-d843ea169333": "langchain-ai/langchain",
  "359d62ef-f693-4db7-a355-bc4756258777": "All-Hands-AI/OpenHands",
  "2bd81b41-d6a7-4dc5-9da5-b913595956a0": "NousResearch/hermes-agent",
};

const DATA_DIR = "/opt/review-dashboard/data";
const STATS_FILE = join(DATA_DIR, "stats.json");

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
  // First run ever - set started_at to now, persists forever
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

async function orbFetch(path: string) {
  const res = await fetch(`${ORB_API}${path}`, {
    headers: { Authorization: `Bearer ${ORB_KEY}` },
    next: { revalidate: 0 },
  });
  return res.json();
}

async function fetchRecentReviews(since: string) {
  const sinceDate = since.slice(0, 19) + "Z";
  const res = await fetch(
    `https://api.github.com/search/issues?q=commenter:nidhishgajjar+%22Orb+Code+Review%22+updated:>${sinceDate}&sort=updated&order=desc&per_page=20`,
    {
      headers: {
        Authorization: `token ${GITHUB_TOKEN}`,
        Accept: "application/vnd.github.v3+json",
      },
      next: { revalidate: 0 },
    }
  );
  if (!res.ok) return { total: 0, items: [] };
  const data = await res.json();
  return {
    total: data.total_count || 0,
    items: (data.items || []).slice(0, 20).map((item: any) => ({
      repo: item.repository_url?.split("/repos/")[1] || "",
      title: item.title,
      number: item.number,
      url: item.html_url,
      updated: item.updated_at,
    })),
  };
}

export async function GET() {
  try {
    const agents: Array<{
      computer_id: string;
      short_id: string;
      repo: string;
      state: string;
    }> = [];

    let running = 0;
    let sleeping = 0;
    let failed = 0;

    for (const [cid, repo] of Object.entries(COMPUTER_REPOS)) {
      try {
        const agentData = await orbFetch(`/computers/${cid}/agents`);
        const agentList = agentData.agents || [];
        const active =
          agentList.find((a: any) => a.state !== "failed") || agentList[0];
        const state = active?.state || "unknown";

        if (state === "running") running++;
        else if (state === "checkpointed") sleeping++;
        else if (state === "failed") failed++;

        agents.push({
          computer_id: cid,
          short_id: cid.slice(0, 8),
          repo,
          state,
        });
      } catch {
        agents.push({
          computer_id: cid,
          short_id: cid.slice(0, 8),
          repo,
          state: "unknown",
        });
      }
    }

    const stats = loadStats();
    stats.total_samples++;
    stats.sleeping_samples += sleeping;
    stats.running_samples += running;
    saveStats(stats);

    const totalAgentSamples = stats.total_samples * 10;
    const sleepingPct =
      totalAgentSamples > 0
        ? Math.round((stats.sleeping_samples / totalAgentSamples) * 100)
        : 0;
    const activePct = 100 - sleepingPct;

    const usage = await orbFetch("/usage");
    const runtimeGbHours = usage.runtime_gb_hours || 0;
    const diskGbHours = usage.disk_gb_hours || 0;
    const costRuntime = runtimeGbHours * 0.005;
    const costDisk = (diskGbHours / 720) * 0.05;
    const totalCost = costRuntime + costDisk;

    const uptimeMs = Date.now() - new Date(stats.started_at).getTime();
    const uptimeHours = Math.round((uptimeMs / 3600000) * 10) / 10;

    const reviewData = await fetchRecentReviews(stats.started_at);

    return NextResponse.json({
      agents,
      stats: {
        total: agents.length,
        running,
        sleeping,
        failed,
        sleeping_pct: sleepingPct,
        active_pct: activePct,
        samples: stats.total_samples,
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
