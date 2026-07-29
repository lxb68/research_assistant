# 任务

把用户提供的错误工具循环决策修复为符合当前模式的合法 JSON。

# 约束

- 只修复结构、字段、枚举和工具参数，不得补充不存在的观察事实。
- 工具名只能来自已注册只读工具，参数必须遵守对应 Schema。
- {{decision_constraint}}
- 只输出合法 JSON，不得输出 Markdown、解释或注释。

# 已注册只读工具

{{tool_catalog}}

{{security_constraint}}
