/* 处理中文和英文分隔符，并去除重复、空白文本。 */

export function splitDelimitedText(value: string): string[] {
  return value
    .split(/\s*(?:,|;|，|；|、)\s*/)
    .map((part) => part.trim())
    .filter(Boolean);
}

export function uniqueTrimmedValues(values: Array<string | undefined>): string[] {
  return Array.from(
    new Set(values.map((value) => value?.trim()).filter(Boolean)),
  ) as string[];
}
