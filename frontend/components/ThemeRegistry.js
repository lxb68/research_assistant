/* 在服务端渲染期间收集 Emotion 样式，并按请求注入到页面。 */

"use client";

import createCache from "@emotion/cache";
import { CacheProvider } from "@emotion/react";
import { createTheme, ThemeProvider } from "@mui/material/styles";
import { useServerInsertedHTML } from "next/navigation";
import { useState } from "react";
import { APP_THEME_STORAGE_KEY } from "@/lib/theme";

/*
 * 让 MUI 的状态层与应用 data-theme 使用同一套明暗色来源。
 * 禁用默认 ripple，避免其 currentColor 蒙层在深浅主题切换时形成白色覆盖块。
 */
const appTheme = createTheme({
  cssVariables: {
    colorSchemeSelector: '[data-theme="%s"]',
    cssVarPrefix: "ra",
  },
  colorSchemes: {
    light: {
      palette: {
        mode: "light",
        primary: { main: "#2a5caa" },
        secondary: { main: "#8b5cf6" },
        background: { default: "#f8fbff", paper: "#ffffff" },
        text: { primary: "#172033", secondary: "#687386" },
        divider: "rgba(42, 92, 170, 0.16)",
      },
    },
    dark: {
      palette: {
        mode: "dark",
        primary: { main: "#60a5fa" },
        secondary: { main: "#a78bfa" },
        background: { default: "#05070c", paper: "#0f172a" },
        text: { primary: "#f8fafc", secondary: "rgba(203, 213, 225, 0.66)" },
        divider: "rgba(148, 163, 184, 0.18)",
      },
    },
  },
  shape: { borderRadius: 8 },
  components: {
    MuiButtonBase: {
      defaultProps: { disableRipple: true },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          position: "relative",
          isolation: "isolate",
          overflow: "hidden",
          backgroundClip: "padding-box",
          boxShadow: "none",
          textTransform: "none",
          transition: "border-color 150ms ease, background 150ms ease, color 150ms ease, box-shadow 150ms ease",
          "&.Mui-disabled": {
            borderColor: "var(--app-border)",
            background: "var(--app-surface-soft)",
            color: "var(--app-text-muted)",
            opacity: 0.62,
          },
        },
        contained: {
          border: "1px solid transparent",
          background: "var(--app-accent-gradient)",
          color: "#ffffff",
          "&:hover": {
            background: "var(--app-accent-gradient)",
            boxShadow: "0 8px 20px var(--app-focus-ring)",
          },
        },
        outlined: {
          borderColor: "var(--app-border-strong)",
          background: "var(--app-surface-soft)",
          color: "var(--app-primary)",
          "&:hover": {
            borderColor: "var(--app-primary)",
            background: "var(--app-primary-soft)",
          },
        },
        text: {
          background: "transparent",
          color: "var(--app-primary)",
          "&:hover": { background: "var(--app-primary-soft)" },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          borderColor: "var(--app-border)",
          color: "var(--app-text)",
        },
        outlined: {
          background: "var(--app-surface-soft)",
        },
        clickable: {
          "&:hover": { background: "var(--app-primary-soft)" },
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
        },
      },
    },
  },
});

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

  return (
    <CacheProvider value={cache}>
      <ThemeProvider
        theme={appTheme}
        defaultMode="light"
        modeStorageKey={APP_THEME_STORAGE_KEY}
        disableTransitionOnChange
      >
        {children}
      </ThemeProvider>
    </CacheProvider>
  );
}
