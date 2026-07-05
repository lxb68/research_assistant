// 后端运行配置集中放在这里，避免端口、跨域等设置散落在业务代码中。
export const config = {
  port: Number(process.env.PORT || 4000),
  corsOrigin: process.env.CORS_ORIGIN || "http://localhost:3000",
};
