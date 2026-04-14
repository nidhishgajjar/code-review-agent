import { NextResponse } from "next/server";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { join } from "path";

const ORB_API = "https://api.orbcloud.dev/v1";
const ORB_KEY = process.env.ORB_API_KEY!;
const GITHUB_TOKEN = process.env.GITHUB_TOKEN!;

// The 10 deployed computers (review@orbcloud.dev org)
const COMPUTER_REPOS: Record<string, string> = {
  "f0b3de5e-c23a-479e-b394-f0299d82e672": "NousResearch/hermes-agent",
  "43fa9828-9f0a-48e8-be97-0b0889fe65f9": "All-Hands-AI/OpenHands",
  "c7088ed0-2df7-43c0-ba38-31b89f4dabde": "langchain-ai/langchain",
  "20785cf8-a139-462f-95d6-be7e73c5b20b": "vercel/next.js",
  "d766498b-1db2-4613-b8f5-12ce85f5a047": "facebook/react",
  "3420a0a4-a567-49b8-a7f7-e92e57a4f2fe": "nodejs/node",
  "070a8e21-0489-4991-9746-3dd29b8f59d1": "fastapi/fastapi",
  "98f4270b-9469-4419-b6c1-8ec8a372224e": "anthropics/anthropic-cookbook",
  "939350c1-4b89-4dc9-889d-ad32dcae14ea": "huggingface/transformers",
  "6ed8e113-b708-47a3-baab-923a723787ff": "microsoft/autogen",
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
