/* 研究记忆与全局用户偏好的客户端 API；组件不直接负责持久化。 */

import { buildApiUrl } from "@/lib/api";

export type ResearchMemoryType = "conclusion" | "fact" | "decision" | "limitation" | "hypothesis" | "task";

export type MemoryEvidence = {
  index?: number;
  recordId?: string;
  title?: string;
  excerpt?: string;
  [key: string]: unknown;
};

export type ResearchMemory = {
  id: string;
  projectId: string;
  projectName: string;
  type: ResearchMemoryType;
  title: string;
  summary: string;
  tags: string[];
  confidence: number;
  evidence: MemoryEvidence[];
  sourceConversationId: string;
  sourceMessageId: string;
  sourceQuestion: string;
  createdAt: string;
  updatedAt: string;
};

export type ResearchMemoryDraft = Omit<ResearchMemory, "id" | "createdAt" | "updatedAt">;

export type UserPreferences = {
  preferredName: string;
  language: string;
  answerStyle: string;
  updatedAt: string;
};

async function parseResponse<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({})) as T & { detail?: string };
  if (!response.ok) throw new Error(payload.detail || `请求失败（${response.status}）`);
  return payload;
}

export async function listResearchMemories(projectIds?: string[]): Promise<ResearchMemory[]> {
  const url = buildApiUrl("/api/research-memories");
  for (const projectId of projectIds ?? []) url.searchParams.append("projectId", projectId);
  const payload = await parseResponse<{ memories?: ResearchMemory[] }>(
    await fetch(url, { cache: "no-store" }),
  );
  return payload.memories ?? [];
}

export async function extractResearchMemory(input: {
  question: string;
  answer: string;
  sources: MemoryEvidence[];
  projectId: string;
  projectName: string;
  sourceConversationId: string;
  sourceMessageId: string;
}): Promise<ResearchMemoryDraft> {
  const payload = await parseResponse<{ candidate: ResearchMemoryDraft }>(
    await fetch(buildApiUrl("/api/research-memories/extract"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
  return payload.candidate;
}

export async function createResearchMemory(draft: ResearchMemoryDraft): Promise<ResearchMemory> {
  const payload = await parseResponse<{ memory: ResearchMemory }>(
    await fetch(buildApiUrl("/api/research-memories"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    }),
  );
  return payload.memory;
}

export async function updateResearchMemory(
  id: string,
  patch: Pick<ResearchMemoryDraft, "title" | "summary" | "type" | "tags" | "confidence">,
): Promise<ResearchMemory> {
  const payload = await parseResponse<{ memory: ResearchMemory }>(
    await fetch(buildApiUrl(`/api/research-memories/${encodeURIComponent(id)}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
  return payload.memory;
}

export async function deleteResearchMemory(id: string): Promise<void> {
  const response = await fetch(buildApiUrl(`/api/research-memories/${encodeURIComponent(id)}`), {
    method: "DELETE",
  });
  if (!response.ok && response.status !== 404) {
    throw new Error(`删除研究记忆失败（${response.status}）`);
  }
}

export async function getUserPreferences(): Promise<UserPreferences> {
  const payload = await parseResponse<{ preferences: UserPreferences }>(
    await fetch(buildApiUrl("/api/user-preferences"), { cache: "no-store" }),
  );
  return payload.preferences;
}

export async function saveUserPreferences(
  patch: Partial<Pick<UserPreferences, "preferredName" | "language" | "answerStyle">>,
): Promise<UserPreferences> {
  const payload = await parseResponse<{ preferences: UserPreferences }>(
    await fetch(buildApiUrl("/api/user-preferences"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
  return payload.preferences;
}

/** 只识别用户明确表达的称呼指令，不推断身份或敏感属性。 */
export function detectExplicitPreferredName(value: string): string {
  const match = value.trim().match(
    /(?:以后|今后|之后)?(?:请)?(?:叫我|称呼我为|称我为|喊我)(?:叫|为)?[“"'「『]?([^，。！？!?\n”"'」』]{1,30})/,
  );
  return match?.[1]?.trim() ?? "";
}

