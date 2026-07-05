# Frontend

这里是独立前端目录，使用 Next.js 构建页面和交互。

## 运行方式

在项目根目录运行：

```powershell
npm.cmd run frontend:dev
```

或者进入前端目录运行：

```powershell
cd E:\research_agent\frontend
npm.cmd run dev
```

默认前端地址：

```text
http://localhost:3000
```

## 目录说明

```text
frontend/
├─ app/              # Next.js App Router 页面和全局布局
├─ components/       # 前端通用组件
├─ home/             # 首页相关组件
├─ public/           # 静态资源
├─ next.config.ts    # Next.js 配置
├─ tsconfig.json     # TypeScript 配置
└─ package.json      # 前端依赖和脚本
```

## 调用后端

后端默认运行在：

```text
http://localhost:4000
```

前端请求后端接口时，可以从：

```text
http://localhost:4000/api/health
http://localhost:4000/api/projects
```

开始对接。
