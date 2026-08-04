/* 验证响应式断点、动态视口和容器边界，防止组件重新引入分散断点。 */

import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const frontendRoot = fileURLToPath(new URL("..", import.meta.url));
const allowedBreakpoints = new Set([520, 760, 960, 1200]);

async function collectCssFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (entry.name === "node_modules" || entry.name === ".next") continue;
    const absolutePath = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await collectCssFiles(absolutePath));
    else if (extname(entry.name) === ".css") files.push(absolutePath);
  }
  return files;
}

test("响应式基础只使用约定的四组宽度断点", async () => {
  const invalid = [];
  for (const file of await collectCssFiles(frontendRoot)) {
    const source = await readFile(file, "utf8");
    const queries = source.matchAll(/@(media|container)[^{]*max-width\s*:\s*(\d+)px/gu);
    for (const query of queries) {
      const width = Number(query[2]);
      if (!allowedBreakpoints.has(width)) invalid.push(`${file}:${width}px`);
    }
  }
  assert.deepEqual(invalid, []);
});

test("TypeScript 与 MUI 使用同一断点映射", async () => {
  const responsiveSource = await readFile(join(frontendRoot, "lib", "responsive.ts"), "utf8");
  const themeSource = await readFile(join(frontendRoot, "components", "ThemeRegistry.js"), "utf8");
  for (const [name, width] of Object.entries({ sm: 520, md: 760, lg: 960, xl: 1200 })) {
    assert.match(responsiveSource, new RegExp(`${name}:\\s*${width}`));
  }
  assert.match(themeSource, /values:\s*RESPONSIVE_BREAKPOINTS/u);
});

test("数据集两个页签共享内容宽度且不依赖 MUI 断点语义", async () => {
  const globalCss = await readFile(join(frontendRoot, "app", "globals.css"), "utf8");
  const datasetSource = await readFile(
    join(frontendRoot, "app", "_views", "DatasetDownloadView.tsx"),
    "utf8",
  );
  assert.match(globalCss, /--dataset-center-content-width:\s*1180px/u);
  assert.match(globalCss, /--dataset-center-inline-space:\s*48px/u);
  assert.match(
    globalCss,
    /\.dataset-browser-panel\s*\{[^}]*max-width:\s*var\(--dataset-center-content-width\)/u,
  );
  assert.match(datasetSource, /<Container\s+disableGutters\s+maxWidth=\{false\}/u);
  assert.match(
    datasetSource,
    /width:\s*"calc\(100% - var\(--dataset-center-inline-space\)\)"/u,
  );
  assert.match(datasetSource, /maxWidth:\s*"var\(--dataset-center-content-width\)"/u);
});

test("本地文献范围在窄屏不会把桌面 flex 基准当作高度", async () => {
  const globalCss = await readFile(join(frontendRoot, "app", "globals.css"), "utf8");
  assert.match(
    globalCss,
    /@media \(max-width: 760px\)[\s\S]*?\.dataset-library-scope label\s*\{[^}]*width:\s*100%;[^}]*flex:\s*0 0 auto;/u,
  );
  assert.match(
    globalCss,
    /\.dataset-library-scope select,\s*\.dataset-library-scope > div\s*\{\s*width:\s*100%;/u,
  );
});

test("项目文献筛选在窄容器不会把桌面 flex 基准当作高度", async () => {
  const globalCss = await readFile(join(frontendRoot, "app", "globals.css"), "utf8");
  assert.match(
    globalCss,
    /@container project-workspace \(max-width: 760px\)[\s\S]*?\.project-literature-filter-fields\s*\{[^}]*width:\s*100%;[^}]*flex:\s*0 0 auto;/u,
  );
});

test("模型预算保留输入项但不显示已删除的冗余文案", async () => {
  const source = await readFile(
    join(frontendRoot, "app", "_views", "project-knowledge", "TokenLimitControl.tsx"),
    "utf8",
  );
  for (const removedText of [
    "模型调用预算",
    "控制分类树 JSON 的单次最大输出",
    "控制每个正文分块的实体与关系 JSON",
    "领域树生成与语义抽取每次等待模型响应的最长时间",
  ]) {
    assert.equal(source.includes(removedText), false);
  }
  for (const inputId of [
    "domain-tree-max-output-tokens",
    "semantic-graph-max-output-tokens",
    "domain-tree-request-timeout-seconds",
  ]) {
    assert.equal(source.includes(inputId), true);
  }
});

test("工作区使用动态视口、命名容器和激活尺寸重测", async () => {
  const globalCss = await readFile(join(frontendRoot, "app", "globals.css"), "utf8");
  const panelSource = await readFile(
    join(frontendRoot, "app", "_components", "WorkspaceViewPanel.tsx"),
    "utf8",
  );
  assert.match(globalCss, /100dvh/u);
  assert.match(globalCss, /container:\s*project-workspace\s*\/\s*inline-size/u);
  assert.match(globalCss, /container:\s*zotero-panel\s*\/\s*inline-size/u);
  assert.match(panelSource, /new ResizeObserver/u);
  assert.match(panelSource, /hidden=\{!isActive\}/u);
});
