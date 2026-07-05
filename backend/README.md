# Backend

这是独立后端服务目录，不依赖 Next.js 的 `app/api`。

## 运行方式

在项目根目录运行：

```powershell
npm.cmd run backend:dev
```

或者进入后端目录运行：

```powershell
cd E:\research_agent\backend
npm.cmd run dev
```

默认服务地址：

```text
http://localhost:4000
```

## 当前接口

```text
GET /api/health
GET /api/projects
POST /api/projects
```

## 目录说明

```text
backend/
├─ src/
│  ├─ server.js          # 后端服务入口
│  ├─ config.js          # 环境变量和服务配置
│  ├─ utils/
│  │  └─ http.js         # HTTP 请求/响应工具函数
│  └─ routes/
│     └─ projects.js     # 项目相关接口
└─ package.json
```
