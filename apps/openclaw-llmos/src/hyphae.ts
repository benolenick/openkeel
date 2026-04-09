/**
 * Hyphae client — LLMOS's long-term memory API.
 *
 * Endpoints used:
 *   POST /recall   { query, top_k, scope? }  -> { results: [{ text, score, ... }] }
 *   POST /remember { text, source, project? } -> { ok: true, id }
 */

import { postJson } from "./http.js";
import type { LlmosConfig } from "./config.js";

export interface HyphaeHit {
  text: string;
  score?: number;
  source?: string;
  project?: string;
  created_at?: string;
}

export interface RecallResponse {
  results: HyphaeHit[];
}

export async function recall(
  cfg: LlmosConfig,
  query: string,
  topK = 10,
  crossProject = false
): Promise<HyphaeHit[]> {
  const body: Record<string, unknown> = { query, top_k: topK };
  if (crossProject) body.scope = {};
  const res = await postJson<RecallResponse>(
    `${cfg.hyphaeUrl}/recall`,
    body,
    cfg.requestTimeoutMs
  );
  return res.results ?? [];
}

export async function remember(
  cfg: LlmosConfig,
  text: string,
  source = "openclaw",
  project?: string
): Promise<{ ok: boolean; id?: string }> {
  const body: Record<string, unknown> = { text, source };
  if (project ?? cfg.recallProject) {
    body.project = project ?? cfg.recallProject;
  }
  return postJson(`${cfg.hyphaeUrl}/remember`, body, cfg.requestTimeoutMs);
}

/**
 * Format recall results as a compact context block for injection into a prompt.
 * Keeps things short — this runs on every inbound message.
 */
export function formatContext(hits: HyphaeHit[], maxChars = 1200): string {
  if (hits.length === 0) return "";
  const lines: string[] = ["[LLMOS memory]"];
  let used = lines[0].length;
  for (const h of hits) {
    const line = `- ${h.text.trim()}`;
    if (used + line.length + 1 > maxChars) break;
    lines.push(line);
    used += line.length + 1;
  }
  return lines.join("\n");
}
