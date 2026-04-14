import { NextResponse } from "next/server";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { join } from "path";

const ORB_API = "https://api.orbcloud.dev/v1";
const ORB_KEY = process.env.ORB_API_KEY!;
const GITHUB_TOKEN = process.env.GITHUB_TOKEN!;

// The 10 deployed computers
const COMPUTER_REPOS: Record<string, string> = {
  "9e29e918-617c-44cd-be79-345bcc427c8f": "NousResearch/hermes-agent",
  "f7bdfe87-3399-464b-926f-ae25ee6d5b8d": "All-Hands-AI/OpenHands",
  "e324011b-f8e2-46ed-8ac3-3c4aafd553d8": "langchain-ai/langchain",
  "7ac90cb2-b406-4ec2-a5d2-5edaf7389e94": "vercel/next.js",
  "c00b7d1e-08cc-46b0-8679-5ff42921981e": "facebook/react",
  "e4617ca0-94f3-4bed-8ecf-dfeba1b109e7": "fastapi/fastapi",
  "3a86a29e-ef31-47ab-8193-c4a797c35656": "anthropics/anthropic-cookbook",
  "c0d57f91-6f93-46fd-86bf-3eefa5867e0d": "huggingface/transformers",
  "36f50fc9-822c-4fb0-b6be-49e8f9c1ac34": "microsoft/autogen",
  "37815c4c-5d35-458f-986f-dec02470c427": "vllm-project/vllm",
};

const STARTED_AT = "2026-04-15T02:30:00Z";
const DATA_DIR = "/opt/review-dashboard/data";
const STATS_FILE = join(DATA_DIR, "stats.json");

// Persistent stats on disk
interface Stats {
  total_samples: number;
  sleeping_samples: number; // sum of sleeping agents per sample
  running_samples: number; // sum of running agents per sample
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
    started_at: STARTED_AT,
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

async function fetchRecentReviews() {
  const res = await fetch(
    "https://api.github.com/search/issues?q=commenter:nidhishgajjar+%22Orb+Code+Review%22&sort=updated&order=desc&per_page=10",
    {
      headers: {
        Authorization: `token ${GITHUB_TOKEN}`,
        Accept: "application/vnd.github.v3+json",
      },
      next: { revalidate: 0 },
    }
  );
  if (!res.ok) return [];
  const data = await res.json();
  return (data.items || []).slice(0, 10).map((item: any) => ({
    repo: item.repository_url?.split("/repos/")[1] || "",
    title: item.title,
    number: item.number,
    url: item.html_url,
    updated: item.updated_at,
  }));
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

    // Accumulate stats to disk
    const stats = loadStats();
    stats.total_samples++;
    stats.sleeping_samples += sleeping;
    stats.running_samples += running;
    saveStats(stats);

    // Calculate percentages (per-agent basis: 10 agents per sample)
    const totalAgentSamples = stats.total_samples * 10;
    const sleepingPct =
      totalAgentSamples > 0
        ? Math.round((stats.sleeping_samples / totalAgentSamples) * 100)
        : 0;
    const activePct = 100 - sleepingPct;

    // Usage from Orb API
    const usage = await orbFetch("/usage");
    const runtimeGbHours = usage.runtime_gb_hours || 0;
    const diskGbHours = usage.disk_gb_hours || 0;
    const costRuntime = runtimeGbHours * 0.005;
    const costDisk = (diskGbHours / 720) * 0.05;
    const totalCost = costRuntime + costDisk;

    // Uptime
    const uptimeMs = Date.now() - new Date(STARTED_AT).getTime();
    const uptimeHours = Math.round((uptimeMs / 3600000) * 10) / 10;

    const reviews = await fetchRecentReviews();

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
      reviews,
      started_at: STARTED_AT,
      timestamp: new Date().toISOString(),
    });
  } catch (error: any) {
    return NextResponse.json(
      { error: error.message || "Failed to fetch status" },
      { status: 500 }
    );
  }
}
