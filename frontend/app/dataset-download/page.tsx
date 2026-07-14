/* 数据集检索与下载功能的独立路由入口。 */

import StandalonePageShell from "@/app/_components/StandalonePageShell";
import DatasetDownloadView from "@/app/_views/DatasetDownloadView";

/** 管理多来源论文检索、筛选和下载。 */
export default function DatasetDownloadPage() {
  return <StandalonePageShell><DatasetDownloadView embedded /></StandalonePageShell>;
}
