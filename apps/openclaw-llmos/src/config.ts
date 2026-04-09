/**
 * Runtime configuration for the openclaw-llmos plugin.
 *
 * All endpoints default to LLMOS's standard localhost ports. Override via env
 * vars when running OpenClaw against a remote LLMOS instance (e.g. LLMOS on
 * kaloth, OpenClaw on a phone-connected VPS).
 */

export interface LlmosConfig {
  hyphaeUrl: string;
  kanbanUrl: string;
  enrichOnMessage: boolean;
  enrichTopK: number;
  enrichMinLength: number;
  recallProject: string | null;
  requestTimeoutMs: number;
}

export function loadConfig(): LlmosConfig {
  const env = process.env;
  return {
    hyphaeUrl: env.LLMOS_HYPHAE_URL ?? "http://127.0.0.1:8100",
    kanbanUrl: env.LLMOS_KANBAN_URL ?? "http://127.0.0.1:8200",
    enrichOnMessage: env.LLMOS_ENRICH_ON_MESSAGE !== "0",
    enrichTopK: parseInt(env.LLMOS_ENRICH_TOP_K ?? "5", 10),
    enrichMinLength: parseInt(env.LLMOS_ENRICH_MIN_LENGTH ?? "12", 10),
    recallProject: env.LLMOS_RECALL_PROJECT ?? null,
    requestTimeoutMs: parseInt(env.LLMOS_TIMEOUT_MS ?? "5000", 10),
  };
}
