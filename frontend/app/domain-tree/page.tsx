import StandalonePageShell from "@/app/_components/StandalonePageShell";
import DomainTreeView from "@/app/_views/DomainTreeView";

export default function DomainTreePage() {
  return <StandalonePageShell><DomainTreeView embedded /></StandalonePageShell>;
}
