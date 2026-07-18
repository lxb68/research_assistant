/* 兼容旧的数据集浏览地址。 */

import { redirect } from "next/navigation";

/** 渲染独立的数据集浏览页面。 */
export default function DatasetBrowserPage() {
  redirect("/datasets?tab=library");
}
