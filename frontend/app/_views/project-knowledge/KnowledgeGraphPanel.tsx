/* 知识图谱视图边界：便于后续独立演进图谱可视化而不影响项目关联。 */

import type { ReactNode } from "react";

export function KnowledgeGraphPanel({ children }: { children: ReactNode }) {
  return <section aria-label="知识图谱分析">{children}</section>;
}
