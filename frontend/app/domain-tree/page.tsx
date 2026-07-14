/* 领域树生成与查看功能的独立路由入口。 */

import StandalonePageShell from "@/app/_components/StandalonePageShell";
import DomainTreeView from "@/app/_views/DomainTreeView";

/** 管理领域树生成、修订和证据关联。 */
export default function DomainTreePage() {
  return <StandalonePageShell><DomainTreeView embedded /></StandalonePageShell>;
}
