/* 数据集中心独立路由，统一承载检索下载与本地文献管理。 */

import StandalonePageShell from "@/app/_components/StandalonePageShell";
import DatasetCenterView, { DatasetCenterTab } from "@/app/_views/DatasetCenterView";

type DatasetCenterPageProps = {
  searchParams: Promise<{ tab?: string | string[] }>;
};

/** 解析初始页签并展示数据集中心。 */
export default async function DatasetCenterPage({ searchParams }: DatasetCenterPageProps) {
  const resolvedSearchParams = await searchParams;
  const rawTab = Array.isArray(resolvedSearchParams.tab)
    ? resolvedSearchParams.tab[0]
    : resolvedSearchParams.tab;
  const initialTab: DatasetCenterTab = rawTab === "library" ? "library" : "download";

  return (
    <StandalonePageShell>
      <DatasetCenterView initialTab={initialTab} />
    </StandalonePageShell>
  );
}
