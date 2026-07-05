# Research Assistant

这是一个前后端分离项目：

- `frontend/`：Next.js 前端应用
- `backend/`：独立 Node.js 后端服务

## Getting Started

启动前端：

```powershell
npm.cmd run frontend:dev
```

启动后端：

```powershell
npm.cmd run backend:dev
```

访问地址：

```text
前端：http://localhost:3000
后端：http://localhost:4000
```

## Project Structure

```text
research_agent/
├─ frontend/       # 前端代码
├─ backend/        # 后端代码
├─ package.json    # 根目录调度脚本
└─ README.md
```

## Scripts

```powershell
npm.cmd run frontend:dev
npm.cmd run frontend:build
npm.cmd run frontend:lint
npm.cmd run backend:dev
npm.cmd run backend:start
```

## Notes

当前后端先使用 Node.js 内置 `http` 模块提供基础接口，方便先完成前后端分离。后续可以继续升级为 Express、Fastify 或 NestJS。
