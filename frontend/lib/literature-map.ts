/* 文献地图前端数据契约与接口适配，隔离页面组件和后端字段格式。 */

import { buildApiUrl } from "@/lib/api";

export type LiteratureMapEvidence = {
  ref: string;
  quote: string;
  section?: string;
  originType?: string;
};

export type LiteratureMapClaim = {
  id: string;
  kind?: string;
  subject: string;
  predicate: string;
  object: string;
  confidence?: number;
  evidence: LiteratureMapEvidence[];
};

export type LiteratureMapCard = {
  paperId: string;
  title: string;
  year?: string;
  summary?: string;
  facets: Record<string, string[]>;
  claims: LiteratureMapClaim[];
  status?: string;
};

export type LiteratureMapRelation = {
  id: string;
  sourcePaperId: string;
  relationType: string;
  targetId: string;
  targetLabel?: string;
  confidence?: number;
  evidence: LiteratureMapEvidence[];
};

export type LiteratureMapStatus =
  | "empty"
  | "ready"
  | "partial"
  | "stale"
  | "building"
  | "failed";

export type LiteratureMapSnapshot = {
  projectId: string;
  status: LiteratureMapStatus;
  generatedAt?: string;
  paperCount: number;
  claimCount: number;
  relationCount: number;
  failedPaperCount: number;
  failedPaperIds: string[];
  cards: LiteratureMapCard[];
  relations: LiteratureMapRelation[];
  error?: string;
};

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown): number | undefined {
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

function normalizeEvidence(value: unknown): LiteratureMapEvidence {
  const item = asRecord(value);
  const recordId = asString(item.record_id ?? item.recordId);
  const chunkIndex = asNumber(item.chunk_index ?? item.chunkIndex);
  return {
    ref: asString(item.ref) || (
      recordId && chunkIndex !== undefined ? `${recordId}:${chunkIndex}` : ""
    ),
    quote: asString(item.quote),
    section: asString(item.section),
    originType: asString(item.origin_type ?? item.originType),
  };
}

function normalizeClaim(value: unknown): LiteratureMapClaim {
  const item = asRecord(value);
  return {
    id: asString(item.id),
    kind: asString(item.kind),
    subject: asString(item.subject),
    predicate: asString(item.predicate),
    object: asString(item.object),
    confidence: asNumber(item.confidence),
    evidence: asArray(item.evidence_refs ?? item.evidenceRefs ?? item.evidence)
      .map(normalizeEvidence)
      .filter((evidence) => Boolean(evidence.quote)),
  };
}

function normalizeCard(value: unknown): LiteratureMapCard {
  const item = asRecord(value);
  const rawFacets = asRecord(item.facets);
  return {
    paperId: asString(item.paper_id ?? item.paperId),
    title: asString(item.title) || "未命名文献",
    year: asString(item.year),
    summary: asString(item.summary),
    facets: Object.fromEntries(
      Object.entries(rawFacets)
        .map(([name, values]) => [
          name,
          asArray(values).map(asString).filter(Boolean),
        ])
        .filter(([, values]) => values.length > 0),
    ),
    claims: asArray(item.claims).map(normalizeClaim),
    status: asString(item.status),
  };
}

function normalizeRelation(value: unknown): LiteratureMapRelation {
  const item = asRecord(value);
  const qualifiers = asRecord(item.qualifiers);
  return {
    id: asString(item.id),
    sourcePaperId: asString(item.source_paper_id ?? item.sourcePaperId),
    relationType: asString(item.relation_type ?? item.relationType),
    targetId: asString(item.target_id ?? item.targetId),
    targetLabel: asString(
      item.target_label ?? item.targetLabel ?? qualifiers.targetLabel,
    ),
    confidence: asNumber(item.confidence),
    evidence: asArray(item.evidence_refs ?? item.evidenceRefs ?? item.evidence)
      .map(normalizeEvidence)
      .filter((evidence) => Boolean(evidence.quote)),
  };
}

function normalizeStatus(value: unknown, cardCount: number): LiteratureMapStatus {
  const status = asString(value);
  if (
    status === "ready"
    || status === "partial"
    || status === "stale"
    || status === "building"
    || status === "failed"
  ) {
    return status;
  }
  return cardCount > 0 ? "ready" : "empty";
}

export function normalizeLiteratureMap(
  projectId: string,
  payload: unknown,
): LiteratureMapSnapshot {
  const item = asRecord(payload);
  const cards = asArray(item.cards ?? item.paper_cards ?? item.paperCards).map(normalizeCard);
  const relations = asArray(item.relations).map(normalizeRelation);
  const claimCount = cards.reduce((total, card) => total + card.claims.length, 0);
  return {
    projectId: asString(item.project_id ?? item.projectId) || projectId,
    status: normalizeStatus(item.status, cards.length),
    generatedAt: asString(item.generated_at ?? item.generatedAt),
    paperCount: asNumber(item.paper_count ?? item.paperCount) ?? cards.length,
    claimCount: asNumber(item.claim_count ?? item.claimCount) ?? claimCount,
    relationCount: asNumber(item.relation_count ?? item.relationCount) ?? relations.length,
    failedPaperCount: asNumber(item.failed_paper_count ?? item.failedPaperCount) ?? 0,
    failedPaperIds: asArray(item.failed_paper_ids ?? item.failedPaperIds)
      .map(asString)
      .filter(Boolean),
    cards,
    relations,
    error: asString(item.error ?? item.error_message ?? item.errorMessage),
  };
}

export async function fetchLiteratureMap(
  projectId: string,
  signal?: AbortSignal,
): Promise<LiteratureMapSnapshot | null> {
  const response = await fetch(
    buildApiUrl(`/api/projects/${encodeURIComponent(projectId)}/literature-map`),
    { signal },
  );
  const payload = await response.json().catch(() => ({}));
  if (response.status === 404) return null;
  if (!response.ok) {
    const detail = asString(asRecord(payload).detail);
    throw new Error(detail || "加载文献地图失败");
  }
  return normalizeLiteratureMap(projectId, payload);
}

export async function buildLiteratureMap(
  projectId: string,
  options: { force: boolean },
): Promise<LiteratureMapSnapshot | null> {
  const response = await fetch(
    buildApiUrl(`/api/projects/${encodeURIComponent(projectId)}/literature-map/build`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: options.force }),
    },
  );
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = asString(asRecord(payload).detail);
    throw new Error(detail || "文献地图构建失败");
  }
  return Object.keys(asRecord(payload)).length > 0
    ? normalizeLiteratureMap(projectId, payload)
    : null;
}
