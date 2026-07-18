# Backend module boundaries

The backend is organized by responsibility rather than by framework entrypoint:

- `api/routes/`: HTTP validation and response mapping only. Routers do not own persistence.
- `api/streaming.py`: bounded adapter between synchronous jobs and NDJSON responses.
- `services/domain_tree_jobs.py`: background task lifecycle and cancellation.
- `services/model_client.py`, `services/mineru.py`, `services/providers/`: external service clients.
- `services/model_config.py`, `services/embedding_store.py`, catalog services: persistence and configuration stores.
- `services/project_repository.py`: project metadata and project-to-paper membership persistence.
- `services/project_scope.py`: trusted project corpus projection for domain analysis and research retrieval.
- `agents/`: research workflows and domain orchestration.
- `schemas/`: transport and domain data models.

`app/main.py` is the composition root. New endpoints belong in a feature router;
new network integrations belong in a service client; durable state belongs in a
store/repository; domain decisions belong in an agent or domain service.

## Project isolation boundary

项目是论文分析的隔离单位。`ProjectRepository` 决定项目论文成员，领域树 Agent 只能消费该成员集合；
领域树、知识图谱、语义缓存和后台任务均以稳定的项目 ID 分区。研究问答在进入检索管线前由
`ProjectScopeService` 过滤论文 ID、历史来源和精确分块引用，生成器和检索器不得自行回退到全局论文。

## Research answer pipeline

研究问答管线按单一职责依次组合，Agent 只保留兼容门面和流程协调：

- `ContextResolver`：投影历史中的指代对象和候选来源，旧回答永不作为事实证据；
- `QuestionContractBuilder`：维护独立问题、允许的论文范围、检索分面和核心声明要求；
- `DocumentStructureIndexer`：读取文档并建立章节、语义结构和连续分块；
- `CandidateRetriever`：执行宽候选召回与排序，不做最终证据截断；
- `EvidenceAssembler`：按逻辑结构、多样性和上下文预算组装证据；
- `EvidenceEvaluator`：判断分面与核心声明的证据支持度；
- `RetrievalRefiner`：把评估缺口转换为有界补偿检索任务；
- `AnswerPolicy`：编译回答深度、边界表述、安全和输出风格规则；
- `AnswerComposer`：根据证据和策略调用模型生成答案；
- `GroundingValidator`：校验最终答案引用集合与核心声明证据组。

允许的主依赖方向为：上下文 → 问题契约 → 结构索引 → 候选召回 → 证据组装 →
证据评估 → 检索补偿或答案生成 → 落地验证。下游组件不得反向修改上游契约。

## Deprecated implementations

The unused `services/minure.py` and `services/mineru_convert.py` prototypes were
removed. `services/mineru.py` is the sole supported MinerU integration.
