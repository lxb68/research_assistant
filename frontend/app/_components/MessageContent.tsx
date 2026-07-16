"use client";

import { ComponentPropsWithoutRef, isValidElement, ReactNode, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { normalizeMarkdownMath } from "@/lib/markdown-math";

type CodeElementProps = { className?: string; children?: ReactNode };

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
  const normalizedContent = normalizeMarkdownMath(content);
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
