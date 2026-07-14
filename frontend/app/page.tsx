/* 解析首页查询参数，并把初始工作区视图交给客户端组件。 */

import HomeWorkspace from "./_components/HomeWorkspace";

type HomePageProps = {
  searchParams: Promise<{ view?: string | string[] }>;
};

export default async function Page({ searchParams }: HomePageProps) {
  // Next.js 16 的 searchParams 是 Promise，必须先等待解析。
  const resolvedSearchParams = await searchParams;
  const view = Array.isArray(resolvedSearchParams.view)
    ? resolvedSearchParams.view[0] ?? null
    : resolvedSearchParams.view ?? null;

  return <HomeWorkspace initialView={view} />;
}
