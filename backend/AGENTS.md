# Backend Agent Guide

适用于 `backend/`。这是 FastAPI 研究服务，负责文献源、PDF/MinerU、Zotero、项目知识、RAG 问答、领域树/知识图谱与持久化后台任务。

## 修改前

- 先查看异常堆栈、应用日志、请求/任务事件和相关测试，再追踪 route → service/agent → repository/provider。
- 完整模块边界见 `app/ARCHITECTURE.md`；修改研究管线前先读该文件。

## 分层边界

- `app/main.py` 是组合根，只配置应用、中间件、生命周期和路由。
- `app/api/routes/` 只做 HTTP 校验、用例调用和响应映射；新路由在 `app/api/routes/__init__.py` 注册。
- `app/api/streaming.py` 只做同步任务到 NDJSON 的有界适配。
- 领域决策、用例编排、仓储、配置和任务系统归 `app/services/`。
- 外部文献源客户端归 `app/services/providers/`；模型/MinerU 等网络调用也应封装为 service client。
- Agent 工作流归 `app/agents/`；不得承担 HTTP 映射或直接持久化。
- 传输/领域结构归 `app/schemas/`；Agent 工具及注册归 `app/tools/`。
- 持久化必须经 repository/store。`prisma/schema.prisma` 不是当前主要状态真源，除非任务明确启用 Prisma。

## 项目隔离

- `ProjectRepository` 决定项目论文成员，`ProjectScopeService` 投影可信检索范围。
- 领域树、知识图谱、语义缓存、文献图谱和后台任务必须按稳定 `project_id` 分区。
- 检索器、生成器和 Agent 不得因项目材料不足而静默回退到全局论文。
- 新缓存键、查询、任务恢复或 API 必须验证项目作用域完整传递。

## 研究问答管线

主依赖方向：

`ContextResolver` → `QuestionContractBuilder` → `DocumentStructureIndexer` → `CandidateRetriever` → `EvidenceAssembler` → `EvidenceEvaluator` → `RetrievalRefiner` / `AnswerComposer` → `GroundingValidator`

- 历史回答只能辅助规划，不能作为事实证据。
- 下游不得反向修改上游契约。
- 召回、组装、充分性评估、补偿检索、答案策略、生成和引用校验保持独立职责。
- 变更管线时优先运行 `tests/test_system_module_boundaries.py`，再补检索、引用、隔离和编排测试。

## 后台任务与配置

- `BackgroundJobManager` 的 SQLite 是任务权威状态，线程池仅执行；遵守容量、取消、重试、心跳、事件序号、清理和恢复约定。
- 禁止新增无界线程池、队列或事件历史。流协议需验证重连、`after` 游标、终态与取消语义。
- 新环境变量同步更新 `.env.example`、解析/校验和测试；不得覆盖 `.env`。
- 日志不得含密钥、授权头或无必要的全文。测试使用 `tmp_path`、mock/fake 和独立 SQLite，不写用户 `storage/`。
- `prompt/` 保存所有静态模型提示与模板，由 `app/prompt_loader.py` 统一读取；变更需验证结构化输出解析、截断和回退路径。

## 验证

测试从 `backend/` 执行，确保 `app` 可导入：

```powershell
cd E:\research_agent\backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pytest  # requirements.txt 仅含运行时依赖
.\.venv\Scripts\python.exe -m pytest tests
```

- 先跑直接相关测试，再扩大测试集。
- 外部服务默认 mock；仅在任务明确要求且凭据已配置时运行真实连接测试。
