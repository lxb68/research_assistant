import assert from "node:assert/strict";
import test from "node:test";

import { normalizeMarkdownMath } from "./markdown-math.ts";

test("normalizes model-generated math delimiters without changing code", () => {
  assert.equal(normalizeMarkdownMath("(\\mathbf{b}^{(k)})"), "$\\mathbf{b}^{(k)}$");
  assert.equal(
    normalizeMarkdownMath("\\(\\mathcal{F}_{\\mathrm{mul}}\\)"),
    "$\\mathcal{F}_{\\mathrm{mul}}$",
  );
  assert.equal(normalizeMarkdownMath("`(\\mathbf{x})`"), "`(\\mathbf{x})`");
  assert.equal(normalizeMarkdownMath("$\\\\mathbf{x}$"), "$\\mathbf{x}$");
});

