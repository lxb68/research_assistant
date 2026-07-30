# 后端模块边界

后端按照职责组织，而不是按照框架入口组织：

- `api/routes/`：只负责 HTTP 校验和响应映射，Router 不负责持久化；
- `api/streaming.py`：同步任务与 NDJSON 响应之间的有界适配器；
- `services/domain_tree_jobs.py`：负责后台任务生命周期和取消；
- `services/model_client.py`、`services/mineru.py`、`services/providers/`：负责外部服务调用；
- `services/model_config.py`、`services/embedding_store.py` 和目录服务：负责持久化与配置存储；
- `services/project_repository.py`：负责项目元数据以及项目与论文成员关系的持久化；
- `services/project_scope.py`：为领域分析和研究检索投影可信的项目语料范围；
- `agents/`：负责研究工作流和领域编排；
- `schemas/`：存放传输层和领域数据模型。

`app/main.py` 是 composition root。新 endpoint 应放入对应的 feature router；新的网络集成应封装为
service client；持久状态应由 store/repository 管理；领域决策应放入 agent 或 domain service。

## 项目隔离边界

项目是论文分析的隔离单位。`ProjectRepository` 决定项目论文成员，领域树 Agent 只能消费该成员集合；
领域树、知识图谱、语义缓存和后台任务均以稳定的项目 ID 分区。研究问答在进入检索管线前由
`ProjectScopeService` 过滤论文 ID、历史来源和精确分块引用，生成器和检索器不得自行回退到全局论文。

## 研究问答管线

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

