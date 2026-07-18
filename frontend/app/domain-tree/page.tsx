/* 项目知识空间的独立路由入口；保留旧路径以兼容已有书签。 */

import StandalonePageShell from "@/app/_components/StandalonePageShell";
import DomainTreeView from "@/app/_views/DomainTreeView";

/** 展示项目文献、领域树和知识图谱。 */
export default function DomainTreePage() {
  return <StandalonePageShell><DomainTreeView embedded /></StandalonePageShell>;
}
