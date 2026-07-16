"use client";

import { ComponentPropsWithoutRef, isValidElement, ReactNode, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

type CodeElementProps = { className?: string; children?: ReactNode };

/**
 * 兼容历史消息中的非标准写法，例如 `(\\mathbf{b}^{(k)}\\in\\{0,1\\}^n)`。
 * remark-math 只识别 `$...$` / `$$...$$` 等标准定界符，故在渲染前补齐定界符。
 */
function normalizeBareLatex(content: string) {
  let normalized = "";
  let cursor = 0;
  while (cursor < content.length) {
    if (content[cursor] !== "(" || content[cursor - 1] === "\\") {
      normalized += content[cursor];
      cursor += 1;
      continue;
    }
    let depth = 0;
    let end = cursor;
    for (; end < content.length; end += 1) {
      if (content[end] === "(") depth += 1;
      if (content[end] === ")") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    if (depth !== 0) {
      normalized += content[cursor];
      cursor += 1;
      continue;
    }
    const candidate = content.slice(cursor + 1, end);
    if (/\\[A-Za-z]+/.test(candidate)) {
      normalized += `$${candidate}$`;
      cursor = end + 1;
      continue;
    }
    normalized += content[cursor];
    cursor += 1;
  }
  return normalized;
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const codeElement = isValidElement<CodeElementProps>(children) ? children : null;
  const code = String(codeElement?.props.children ?? "").replace(/\n$/, "");
  const language = codeElement?.props.className?.match(/language-([^\s]+)/)?.[1] ?? "代码";
  const copy = async () => {
    await navigator.clipboard?.writeText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return <section className="message-code-block">
    <header><span>{language}</span><button type="button" onClick={() => void copy()}>{copied ? "已复制" : "复制"}</button></header>
    <pre>{children}</pre>
  </section>;
}

/** 安全显示聊天 Markdown：默认不解析原始 HTML，因此模型输出不能注入页面。 */
export default function MessageContent({ content }: { content: string }) {
  const normalizedContent = normalizeBareLatex(content);
  return <div className="message-content">
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex, rehypeHighlight]}
      components={{
        pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
        code: ({ className, children, ...props }: ComponentPropsWithoutRef<"code">) => (
          <code className={className} {...props}>{children}</code>
        ),
      }}
    >
      {normalizedContent}
    </ReactMarkdown>
  </div>;
}
