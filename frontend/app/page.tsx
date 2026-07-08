import HomeWorkspace from "./_components/HomeWorkspace";

type HomePageProps = {
  searchParams: Promise<{ view?: string | string[] }>;
};

export default async function Page({ searchParams }: HomePageProps) {
  const resolvedSearchParams = await searchParams;
  const view = Array.isArray(resolvedSearchParams.view)
    ? resolvedSearchParams.view[0] ?? null
    : resolvedSearchParams.view ?? null;

  return <HomeWorkspace initialView={view} />;
}
