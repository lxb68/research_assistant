import http from "node:http";
import { config } from "./config.js";
import { handleProjectsRoute } from "./routes/projects.js";
import { sendJson, setCorsHeaders } from "./utils/http.js";

const server = http.createServer(async (req, res) => {
  setCorsHeaders(res, config.corsOrigin);

  // 浏览器跨域预检请求直接返回，避免真正业务接口重复处理 OPTIONS。
  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url || "/", `http://${req.headers.host}`);

  try {
    // 健康检查接口：用于确认后端服务是否正常启动。
    if (url.pathname === "/api/health") {
      return sendJson(res, 200, {
        status: "ok",
        service: "research-assistant-backend",
        timestamp: new Date().toISOString(),
      });
    }

    // 项目接口：先提供列表和创建能力，方便前端后续对接。
    if (url.pathname === "/api/projects") {
      return handleProjectsRoute(req, res);
    }

    return sendJson(res, 404, {
      error: "接口不存在",
    });
  } catch (error) {
    return sendJson(res, error.statusCode || 500, {
      error: error.message || "服务器内部错误",
    });
  }
});

server.listen(config.port, () => {
  console.log(`Backend API is running at http://localhost:${config.port}`);
});
