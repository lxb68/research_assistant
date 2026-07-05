// Prisma CLI 配置文件：统一指定 schema、迁移目录和数据库连接地址。
import "dotenv/config";
import { defineConfig } from "prisma/config";

export default defineConfig({
  // schema 路径相对于 backend/ 目录。
  schema: "prisma/schema.prisma",
  migrations: {
    path: "prisma/migrations",
  },
  datasource: {
    // DATABASE_URL 建议写在 backend/.env 中，例如：file:./dev.db
    url: process.env["DATABASE_URL"],
  },
});
