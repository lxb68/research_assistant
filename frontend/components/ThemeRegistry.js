/**
 * 预留的主题注册组件。
 *
 * 当前项目尚未安装 MUI、next-themes 等主题依赖，所以先保持为轻量包裹组件。
 * 后续如果接入完整主题系统，可以在这里统一放置 ThemeProvider。
 */
export default function ThemeRegistry({ children }) {
  return children;
}
