# Frontend Agent Guide

适用于 `frontend/`。这是 Next.js 16.2 App Router 前端，React 19 + TypeScript strict + MUI/Emotion；负责项目、文献、研究问答、领域树、知识图谱与后台任务 UI。

## 修改前

- 此版本 Next.js 与既有知识可能不兼容。写代码前必须阅读 `node_modules/next/dist/docs/` 中与任务相关的指南，并遵循弃用提示。
- 先沿“页面/组件 → `lib/` 请求或流适配 → 后端契约”定位问题；检查浏览器报错、网络响应、状态生命周期和 Server/Client 边界。

## 边界

- `app/page.tsx`、路由页和 layout 保持轻量，页面主体组合 `app/_views/`。
- `app/_components/` 放应用级共享组件与 Provider；`components/` 放跨页面通用组件。
- 项目知识面板归 `app/_views/project-knowledge/`；不要把业务逻辑塞回路由页。
- API 地址、请求、流协议、领域类型和纯函数归 `lib/`。组件不得手拼后端 URL、重复解析协议或持有服务端持久化逻辑。
- API 基础地址统一经 `lib/constants.ts` 与 `lib/api.ts`。
- 默认使用 Server Component；仅在状态、事件、浏览器 API 或客户端上下文需要时添加 `"use client"`，并缩小客户端边界。
- 服务端密钥和 server-only 模块不得进入客户端依赖图；仅明确公开的变量使用 `NEXT_PUBLIC_`。
- 沿用现有 MUI/Emotion 主题与组件模式。样式放相邻 CSS Module 或既有主题层，不引入平行设计体系。
- `app/dataset-brower/` 是现存公开路由拼写；未做完整迁移与兼容前不要顺手重命名。

## 验证

按影响范围执行：

```powershell
# 项目根目录
npm.cmd run frontend:lint
npm.cmd run frontend:build
node --test frontend/lib/markdown-math.test.mjs
```

- 类型、路由、Next 配置、Server/Client 边界变更必须跑 build。
- 纯 UI 变更至少检查加载、空、错误、长文本和窄屏状态。
- 流式响应与后台任务变更需检查断线重连、增量游标、终态和取消展示。
