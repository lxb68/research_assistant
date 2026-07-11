"use client";

import ResearchChat from "@/app/_components/ResearchChat";
import StandalonePageShell from "@/app/_components/StandalonePageShell";
import { useRouter } from "next/navigation";

export default function ResearchChatPage() {
  const router = useRouter();

  return (
    <StandalonePageShell>
      <ResearchChat
        onOpenDownload={() => router.push("/dataset-download")}
        onOpenBrowse={() => router.push("/dataset-brower")}
        onOpenDomainTree={() => router.push("/domain-tree")}
        onOpenSettings={() => router.push("/setting")}
      />
    </StandalonePageShell>
  );
}
