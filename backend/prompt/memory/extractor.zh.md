# 任务

从用户提供的研究对话或结果中，提取一条未来研究可复用的记忆；不要复述整条回答。

# 选择标准

- 只保留未来可能影响检索、分析、决策或执行的信息。
- 必须保留适用范围、前提、例外和不确定性等关键限定。
- 没有足够明确且可复用的信息时，返回语义保守的结果，不得补充或推测事实。

# 输出约束

- 只输出一个合法 JSON 对象，字段固定为 `title`、`summary`、`type`、`tags`、`confidence`。
- `type` 只能是 `conclusion|fact|decision|limitation|hypothesis|task`。
- `summary` 不超过 500 个汉字。
- `tags` 最多 8 个，去除重复和过度宽泛标签。
- `confidence` 为 0 到 1。
- 不得输出 Markdown、解释或额外字段。

# 输出格式

```json
{"title":"","summary":"","type":"fact","tags":[],"confidence":0.0}
```

# 输出前自检

- 内容可在未来独立理解和复用。
- 限定条件没有被删去，事实与推测没有混淆。
- JSON 可直接解析，字段和枚举值完全符合约束。
