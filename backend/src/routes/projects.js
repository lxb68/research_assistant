import { readJsonBody, sendJson } from "../utils/http.js";

// 临时内存数据：先用于打通前后端流程，后续可以替换为数据库。
const projects = [
  {
    id: "demo-project",
    name: "示例研究项目",
    description: "这是一个后端返回的示例项目，用于验证前后端分离接口。",
    createdAt: new Date().toISOString(),
  },
];

// 项目路由处理函数：集中管理 /api/projects 相关接口。
export async function handleProjectsRoute(req, res) {
  if (req.method === "GET") {
    return sendJson(res, 200, {
      data: projects,
    });
  }

  if (req.method === "POST") {
    const body = await readJsonBody(req);

    if (!body.name || typeof body.name !== "string") {
      return sendJson(res, 400, {
        error: "项目名称不能为空",
      });
    }

    const project = {
      id: crypto.randomUUID(),
      name: body.name,
      description: body.description || "",
      createdAt: new Date().toISOString(),
    };

    projects.push(project);

    return sendJson(res, 201, {
      data: project,
    });
  }

  return sendJson(res, 405, {
    error: "当前接口不支持该请求方法",
  });
}
