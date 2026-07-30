# Task

Translate every Chinese research query into concise English academic search terms.

# Output

Return one JSON object whose `translations` field is an array. Preserve each input `id` exactly and return one object per input:

```json
{"translations":[{"id":"q1","query":"translated academic search terms"}]}
```

Do not answer the research questions. Do not add explanations or Markdown.
