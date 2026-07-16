/**
 * Normalize common model-generated LaTeX delimiters without touching code.
 *
 * remark-math accepts `$...$` and `$$...$$`, while models and imported
 * documents also commonly emit `\(...\)`, `\[...\]`, or bare
 * `(\mathbf{x})`. Keeping this compatibility at the Markdown boundary makes
 * the renderer independent from any particular model provider.
 */

function normalizeLatexCommands(value: string) {
  // JSON-looking examples in prompts sometimes make a model emit two literal
  // backslashes before a command. A TeX line break is followed by whitespace,
  // so collapsing only command-prefixed pairs preserves intentional breaks.
  return value.replace(/\\\\(?=[A-Za-z]+)/g, "\\");
}

function normalizeTextSegment(content: string) {
  let normalized = "";
  let cursor = 0;
  let mathDelimiter: "$" | "$$" | null = null;

  while (cursor < content.length) {
    if (content[cursor] === "$" && content[cursor - 1] !== "\\") {
      const delimiter = content[cursor + 1] === "$" ? "$$" : "$";
      if (mathDelimiter === null) mathDelimiter = delimiter;
      else if (mathDelimiter === delimiter) mathDelimiter = null;
      normalized += delimiter;
      cursor += delimiter.length;
      continue;
    }

    if (mathDelimiter) {
      if (content[cursor] === "\\" && content[cursor + 1] === "\\" && /[A-Za-z]/.test(content[cursor + 2] ?? "")) {
        normalized += "\\";
        cursor += 2;
        continue;
      }
      normalized += content[cursor];
      cursor += 1;
      continue;
    }

    const escapedDelimiter = content.slice(cursor, cursor + 2);
    if (escapedDelimiter === "\\(" || escapedDelimiter === "\\[") {
      const closing = escapedDelimiter === "\\(" ? "\\)" : "\\]";
      const end = content.indexOf(closing, cursor + 2);
      if (end >= 0) {
        const delimiter = escapedDelimiter === "\\(" ? "$" : "$$";
        const candidate = normalizeLatexCommands(content.slice(cursor + 2, end));
        normalized += `${delimiter}${candidate}${delimiter}`;
        cursor = end + 2;
        continue;
      }
    }

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
    if (depth === 0) {
      const candidate = content.slice(cursor + 1, end);
      if (/\\{1,2}[A-Za-z]+/.test(candidate)) {
        normalized += `$${normalizeLatexCommands(candidate)}$`;
        cursor = end + 1;
        continue;
      }
    }

    normalized += content[cursor];
    cursor += 1;
  }
  return normalized;
}

export function normalizeMarkdownMath(content: string) {
  let normalized = "";
  let cursor = 0;

  while (cursor < content.length) {
    if (content[cursor] !== "`") {
      const nextCode = content.indexOf("`", cursor);
      const end = nextCode >= 0 ? nextCode : content.length;
      normalized += normalizeTextSegment(content.slice(cursor, end));
      cursor = end;
      continue;
    }

    let ticks = 1;
    while (content[cursor + ticks] === "`") ticks += 1;
    const delimiter = "`".repeat(ticks);
    const end = content.indexOf(delimiter, cursor + ticks);
    if (end < 0) {
      // During streaming, an unfinished code span must remain literal.
      normalized += content.slice(cursor);
      break;
    }
    normalized += content.slice(cursor, end + ticks);
    cursor = end + ticks;
  }

  return normalized;
}

