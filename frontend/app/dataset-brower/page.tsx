/* 数据集浏览功能的独立路由入口。 */

import StandalonePageShell from "@/app/_components/StandalonePageShell";
import DatasetBrowserView from "@/app/_views/DatasetBrowserView";

export default function DatasetBrowserPage() {
  return <StandalonePageShell><DatasetBrowserView /></StandalonePageShell>;
}
