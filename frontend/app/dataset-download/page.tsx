/* 兼容旧的数据集下载地址。 */

import { redirect } from "next/navigation";

/** 管理多来源论文检索、筛选和下载。 */
export default function DatasetDownloadPage() {
  redirect("/datasets?tab=download");
}
