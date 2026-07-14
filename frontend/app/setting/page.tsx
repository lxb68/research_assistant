/* 工作区设置功能的独立路由入口。 */

import StandalonePageShell from "@/app/_components/StandalonePageShell";
import SettingsView from "@/app/_views/SettingsView";

export default function SettingPage() {
  return <StandalonePageShell><SettingsView /></StandalonePageShell>;
}
