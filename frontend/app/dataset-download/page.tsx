/* 数据集检索与下载功能的独立路由入口。 */

import StandalonePageShell from "@/app/_components/StandalonePageShell";
import DatasetDownloadView from "@/app/_views/DatasetDownloadView";

export default function DatasetDownloadPage() {
  return <StandalonePageShell><DatasetDownloadView embedded /></StandalonePageShell>;
}
