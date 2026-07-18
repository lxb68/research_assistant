/* 领域树视图边界：领域树特有的交互和结果均收敛在该组件内。 */

import type { ReactNode } from "react";

export function DomainTreePanel({ children }: { children: ReactNode }) {
  return <section aria-label="领域树分析">{children}</section>;
}
