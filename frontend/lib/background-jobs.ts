/* 后台任务中心的共享 API 类型。 */

import { buildApiUrl } from "@/lib/api";

export type BackgroundJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelling"
  | "cancelled"
  | "interrupted";

export type BackgroundJob = {
  jobId: string;
  type: string;
  status: BackgroundJobStatus;
  stage: string;
  progress: number;
  message: string;
  request?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string;
  retryable?: boolean;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
};

export type BackgroundJobEvent = {
  sequence: number;
  jobId: string;
  type: string;
  payload: Record<string, unknown>;
  createdAt: string;
};

export async function fetchJob(jobId: string): Promise<BackgroundJob> {
  const response = await fetch(buildApiUrl(`/api/jobs/${jobId}`), { cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "读取后台任务失败");
  return payload as BackgroundJob;
}

export async function waitForJob(
  jobId: string,
  onUpdate?: (job: BackgroundJob) => void,
): Promise<BackgroundJob> {
  while (true) {
    const job = await fetchJob(jobId);
    onUpdate?.(job);
    if (["completed", "failed", "cancelled", "interrupted"].includes(job.status)) return job;
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
  }
}
