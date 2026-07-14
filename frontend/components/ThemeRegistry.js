/* 在服务端渲染期间收集 Emotion 样式，并按请求注入到页面。 */

"use client";

import createCache from "@emotion/cache";
import { CacheProvider } from "@emotion/react";
import { useServerInsertedHTML } from "next/navigation";
import { useState } from "react";

/** 收集并注入服务端渲染所需的 Emotion 样式。 */
export default function ThemeRegistry({ children }) {
  const [{ cache, flush }] = useState(() => {
    const cache = createCache({ key: "css" });
    cache.compat = true;
    const previousInsert = cache.insert;
    let inserted = [];

    // 记录本轮渲染新增的样式名，避免重复输出已缓存样式。
    cache.insert = (...args) => {
      const serialized = args[1];
      if (cache.inserted[serialized.name] === undefined) {
        inserted.push(serialized.name);
      }
      return previousInsert(...args);
    };

    /** 返回本轮新增样式名，并清空待注入队列。 */
    const flush = () => {
      const previous = inserted;
      inserted = [];
      return previous;
    };

    return { cache, flush };
  });

  useServerInsertedHTML(() => {
    const names = flush();
    if (names.length === 0) {
      return null;
    }

    let styles = "";
    for (const name of names) {
      styles += cache.inserted[name];
    }

    return (
      <style
        data-emotion={`${cache.key} ${names.join(" ")}`}
        dangerouslySetInnerHTML={{ __html: styles }}
      />
    );
  });

  return <CacheProvider value={cache}>{children}</CacheProvider>;
}
