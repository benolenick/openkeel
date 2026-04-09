/**
 * Kanban client — LLMOS's project task tracker (OpenKeel Command Board).
 *
 * Endpoints used:
 *   POST /api/task                       create
 *   POST /api/task/{id}/move             move between columns
 *   POST /api/task/{id}/report            report agent progress
 *   GET  /api/board/{board}              list
 */

import { postJson, getJson } from "./http.js";
import type { LlmosConfig } from "./config.js";

export type TaskStatus = "todo" | "in_progress" | "done" | "blocked";
export type TaskPriority = "low" | "medium" | "high";

export interface TaskCreateInput {
  title: string;
  description?: string;
  status?: TaskStatus;
  priority?: TaskPriority;
  type?: string;
  project?: string;
  board?: string;
}

export interface Task {
  id: number;
  title: string;
  description?: string;
  status: TaskStatus;
  priority?: TaskPriority;
  board?: string;
  project?: string;
}

export async function createTask(
  cfg: LlmosConfig,
  input: TaskCreateInput
): Promise<Task> {
  const body = {
    title: input.title,
    description: input.description ?? "",
    status: input.status ?? "todo",
    priority: input.priority ?? "medium",
    type: input.type ?? "task",
    project: input.project ?? "personal",
    board: input.board ?? "default",
  };
  return postJson<Task>(`${cfg.kanbanUrl}/api/task`, body, cfg.requestTimeoutMs);
}

export async function moveTask(
  cfg: LlmosConfig,
  taskId: number,
  status: TaskStatus
): Promise<{ ok: boolean }> {
  return postJson(
    `${cfg.kanbanUrl}/api/task/${taskId}/move`,
    { status },
    cfg.requestTimeoutMs
  );
}

export async function reportTask(
  cfg: LlmosConfig,
  taskId: number,
  status: "done" | "blocked" | "in_progress",
  report: string,
  agentName = "openclaw"
): Promise<{ ok: boolean }> {
  return postJson(
    `${cfg.kanbanUrl}/api/task/${taskId}/report`,
    { agent_name: agentName, status, report },
    cfg.requestTimeoutMs
  );
}

export async function listBoard(
  cfg: LlmosConfig,
  board = "default"
): Promise<Task[]> {
  const res = await getJson<{ tasks?: Task[] } | Task[]>(
    `${cfg.kanbanUrl}/api/board/${board}`,
    cfg.requestTimeoutMs
  );
  if (Array.isArray(res)) return res;
  return res.tasks ?? [];
}
