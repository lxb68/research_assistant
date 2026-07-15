/* 组织多来源论文检索、筛选、下载进度与结果持久化界面。 */

"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Container,
  Divider,
  Paper,
  Snackbar,
  Slider,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ErrorOutlinedIcon from "@mui/icons-material/ErrorOutlined";
import RadioButtonUncheckedIcon from "@mui/icons-material/RadioButtonUnchecked";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import SearchIcon from "@mui/icons-material/Search";
import { buildApiUrl } from "@/lib/api";
import { readNdjsonStream } from "@/lib/stream";

type PaperSource = "arxiv" | "pubmed" | "crossref" | "ieee" | "open_access";
type SourceState = "idle" | "searching" | "done" | "error";

type SourceProgress = {
  source: PaperSource;
  state: SourceState;
  message: string;
  count: number;
  logs?: string[];
};

type PaperResult = {
  id?: string;
  source: string;
  title: string;
  authors?: string[];
  abstract?: string;
  year?: string;
  venue?: string;
  doi?: string;
  url?: string;
  pdfUrl?: string;
  pdfPath?: string;
  pdfDownloadError?: string;
  requiresManualDownload?: boolean;
  manualDownloadReason?: string;
  preprintSource?: boolean;
  metricFiltersIgnored?: boolean;
  relevanceScore?: number;
  impactFactor?: number | null;
  ccfLevel?: string;
  externalId?: string;
};

type DatasetDownloadResponse = {
  keyword: string;
  searchKeyword?: string;
  sources: PaperSource[];
  targetPerSource?: number;
  targetCount?: number;
  searchedCount: number;
  deduplicatedCount: number;
  filteredCount: number;
  savedCount: number;
  savedCountsBySource?: Partial<Record<PaperSource, number>>;
  errors: Array<{
    source: PaperSource;
    message: string;
  }>;
  logs?: string[];
  papers: PaperResult[];
};

type StreamEvent =
  | { type: "log"; message: string }
  | { type: "result"; result: DatasetDownloadResponse }
  | { type: "error"; message: string }
  | { type: "done" };

const paperSources: Array<{
  id: PaperSource;
  label: string;
  description: string;
  requiresKey?: boolean;
}> = [
  {
    id: "arxiv",
    label: "arXiv",
    description: "预印本文献，适合计算机、数学、物理等方向。",
  },
  {
    id: "pubmed",
    label: "PubMed",
    description: "生物医学文献来源，建议后端配置 NCBI_EMAIL。",
  },
  {
    id: "crossref",
    label: "Crossref",
    description: "DOI 与出版元数据，适合补全论文基础信息。",
  },
  {
    id: "ieee",
    label: "IEEE",
    description: "工程和计算机方向文献，需要后端配置 IEEE_API_KEY。",
    requiresKey: true,
  },
  {
    id: "open_access",
    label: "Open Access",
    description: "合法开放获取论文，优先使用 OpenAlex 提供的开放 PDF 链接。",
  },
];

const defaultSelectedSources: PaperSource[] = ["arxiv", "crossref", "open_access"];
/** 返回论文来源对应的中文名称。 */
function getSourceLabel(source: PaperSource) {
  return paperSources.find((item) => item.id === source)?.label ?? source;
}

/** 为选中来源创建初始进度记录。 */
function createProgressForSources(sources: PaperSource[]): SourceProgress[] {
  // 每次检索都为选中来源创建独立状态，便于流式更新进度。
  return sources.map((source) => ({
    source,
    state: "idle",
    message: "等待搜索",
    count: 0,
  }));
}

/** 把来源状态映射为进度图标和颜色。 */
function getProgressVisual(state: SourceState) {
  // 将后端状态映射为统一的图标和界面颜色。
  switch (state) {
    case "done":
      return {
        icon: <CheckCircleIcon fontSize="small" />,
        bg: "rgba(16, 185, 129, 0.16)",
        border: "rgba(52, 211, 153, 0.42)",
        color: "#a7f3d0",
      };
    case "searching":
      return {
        icon: <CircularProgress size={16} thickness={5} />,
        bg: "rgba(37, 99, 235, 0.18)",
        border: "rgba(96, 165, 250, 0.48)",
        color: "#bfdbfe",
      };
    case "error":
      return {
        icon: <ErrorOutlinedIcon fontSize="small" />,
        bg: "rgba(225, 29, 72, 0.16)",
        border: "rgba(251, 113, 133, 0.48)",
        color: "#fecdd3",
      };
    default:
      return {
        icon: <RadioButtonUncheckedIcon fontSize="small" />,
        bg: "rgba(15, 23, 42, 0.54)",
        border: "rgba(148, 163, 184, 0.24)",
        color: "#cbd5e1",
      };
  }
}

type DatasetDownloadPageProps = {
  embedded?: boolean;
  isActiveView?: boolean;
};

/** 管理多来源论文检索、筛选和下载。 */
export default function DatasetDownloadPage({
  embedded = false,
  isActiveView = true,
}: DatasetDownloadPageProps = {}) {
  const [query, setQuery] = useState("");
  const [selectedSources, setSelectedSources] = useState<PaperSource[]>(defaultSelectedSources);
  const [limitPerSource, setLimitPerSource] = useState(10);
  const [isSearching, setIsSearching] = useState(false);
  const [results, setResults] = useState<PaperResult[]>([]);
  const [yearFrom, setYearFrom] = useState("2020");
  const [yearTo, setYearTo] = useState("2026");
  const [minImpactFactor, setMinImpactFactor] = useState(0);
  const [selectedCcfLevels, setSelectedCcfLevels] = useState<string[]>([]);
  const [agentLogs, setAgentLogs] = useState<string[]>([]);
  const [localPdfPaths, setLocalPdfPaths] = useState<Record<string, string>>({});
  const [linkingPaperId, setLinkingPaperId] = useState("");
  const [isCleaningMetadata, setIsCleaningMetadata] = useState(false);
  const [progress, setProgress] = useState<SourceProgress[]>(
    createProgressForSources(defaultSelectedSources),
  );
  const [error, setError] = useState("");
  const [summary, setSummary] = useState("");
  const [completionNoticeOpen, setCompletionNoticeOpen] = useState(false);
  const [completionNoticeMessage, setCompletionNoticeMessage] = useState("");
  const activeViewRef = useRef(isActiveView);
  const glassPanel = {
    border: "1px solid rgba(148, 163, 184, 0.22)",
    background: "rgba(8, 13, 24, 0.68)",
    backdropFilter: "blur(18px)",
    boxShadow: "0 28px 70px rgba(0, 0, 0, 0.34)",
  };
  const darkTextFieldSx = {
    "& .MuiInputLabel-root": {
      color: "rgba(226, 232, 240, 0.72)",
    },
    "& .MuiInputLabel-root.Mui-focused": {
      color: "#93c5fd",
    },
    "& .MuiOutlinedInput-root": {
      color: "#f8fafc",
      background: "rgba(15, 23, 42, 0.44)",
      "& fieldset": {
        borderColor: "rgba(148, 163, 184, 0.3)",
      },
      "&:hover fieldset": {
        borderColor: "rgba(147, 197, 253, 0.55)",
      },
      "&.Mui-focused fieldset": {
        borderColor: "#60a5fa",
      },
    },
    "& .MuiInputBase-input::placeholder": {
      color: "rgba(203, 213, 225, 0.62)",
      opacity: 1,
    },
  };

  const selectedSourceText = useMemo(
    () => selectedSources.map(getSourceLabel).join(" / "),
    [selectedSources],
  );

  const selectedProgress = useMemo(
    () =>
      selectedSources.map(
        (source) =>
          progress.find((item) => item.source === source) ?? {
            source,
            state: "idle" as SourceState,
            message: "等待搜索",
            count: 0,
            logs: [],
          },
      ),
    [progress, selectedSources],
  );

  const selectedArxiv = selectedSources.includes("arxiv");
  const selectedPublishedSources = selectedSources.filter((source) => source !== "arxiv");

  const manualDownloadCount = useMemo(
    () => results.filter((paper) => paper.requiresManualDownload || !paper.pdfPath).length,
    [results],
  );

  useEffect(() => {
    activeViewRef.current = isActiveView;
  }, [isActiveView]);

  /** 切换论文检索来源。 */
  function toggleSource(source: PaperSource) {
    setSelectedSources((current) => {
      const next = current.includes(source)
        ? current.filter((item) => item !== source)
        : [...current, source];

      return next.length > 0 ? next : current;
    });
  }

  /** 切换 CCF 等级筛选条件。 */
  function toggleCcfLevel(level: string) {
    setSelectedCcfLevels((current) =>
      current.includes(level) ? current.filter((item) => item !== level) : [...current, level],
    );
  }

  /** 合并更新指定来源的检索进度。 */
  function updateProgress(source: PaperSource, patch: Partial<SourceProgress>) {
    setProgress((current) => {
      const exists = current.some((item) => item.source === source);
      const fallback: SourceProgress = {
        source,
        state: "idle",
        message: "等待搜索",
        count: 0,
        ...patch,
      };
      const next = exists
        ? current.map((item) => (item.source === source ? { ...item, ...patch } : item))
        : [...current, fallback];

      return next;
    });
  }

  /** 发起流式数据集检索并处理进度事件。 */
  async function streamDataset(
    keyword: string,
    limit: number,
    onStreamEvent: (event: StreamEvent) => void,
  ) {
    const response = await fetch(buildApiUrl("/api/datasets/download/stream"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        keyword,
        sources: selectedSources,
        limit_per_source: limit,
        download_pdf: true,
        year_from: yearFrom ? Number(yearFrom) : null,
        year_to: yearTo ? Number(yearTo) : null,
        min_impact_factor: minImpactFactor > 0 ? minImpactFactor : null,
        ccf_levels: selectedCcfLevels,
      }),
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 404) {
        throw new Error(
          "后端没有找到流式接口 /api/datasets/download/stream。请重启后端，并确认使用 app.main:app 启动。",
        );
      }
      throw new Error(payload.detail || payload.error || "HunterAgent 下载数据集失败");
    }

    if (!response.body) {
      throw new Error("浏览器无法读取后端流式响应");
    }

    // 回调中的直接赋值不会参与 TypeScript 的跨函数控制流分析，因此用结果数组显式承接最终事件。
    const finalResults: DatasetDownloadResponse[] = [];

    await readNdjsonStream<StreamEvent>(response.body, (event) => {
      onStreamEvent(event);
      if (event.type === "result") {
        finalResults.push({
          ...event.result,
          errors: event.result.errors ?? [],
          papers: event.result.papers ?? [],
        });
      }
      if (event.type === "error") {
        throw new Error(event.message);
      }
    });
    const finalResult = finalResults.at(-1);
    if (!finalResult) {
      throw new Error("后端没有返回最终结果");
    }

    return finalResult;
  }

  /** 筛选属于指定来源的运行日志。 */
  function getLogsForSource(logs: string[], source: PaperSource) {
    const sourceKey = `[${source}]`;
    return logs.filter((log) => log.includes(sourceKey)).slice(-4);
  }

  /** 从日志前缀识别对应论文来源。 */
  function getSourceFromLog(log: string): PaperSource | null {
    const bracketMatch = log.match(/^\[(arxiv|pubmed|crossref|ieee|open_access)\]/i);
    if (bracketMatch) {
      return bracketMatch[1].toLowerCase() as PaperSource;
    }

    const searchMatch = log.match(/数据源\s+(arxiv|pubmed|crossref|ieee|open_access)/i);
    if (searchMatch) {
      return searchMatch[1].toLowerCase() as PaperSource;
    }

    return null;
  }

  /** 把检索表单和结果恢复为初始状态。 */
  function handleReset() {
    setQuery("");
    setSelectedSources(defaultSelectedSources);
    setLimitPerSource(10);
    setYearFrom("2020");
    setYearTo("2026");
    setMinImpactFactor(0);
    setSelectedCcfLevels([]);
    setResults([]);
    setAgentLogs([]);
    setLocalPdfPaths({});
    setLinkingPaperId("");
    setIsCleaningMetadata(false);
    setError("");
    setSummary("");
    setProgress(createProgressForSources(defaultSelectedSources));
  }

  /** 请求后端清理缺失 PDF 的元数据。 */
  async function cleanupMissingPdfs() {
    setIsCleaningMetadata(true);
    setError("");

    try {
      const response = await fetch(buildApiUrl("/api/papers/cleanup-missing-pdfs"), {
        method: "POST",
      });
      const payload = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(payload.detail || "清理无 PDF 元数据失败");
      }

      const removedIds = new Set<string>(
        (payload.removedRecords || [])
          .map((record: PaperResult) => record.id)
          .filter(Boolean),
      );

      setResults((current) => current.filter((paper) => !paper.id || !removedIds.has(paper.id)));
      setSummary(
        `清理完成：删除 ${payload.removedCount ?? 0} 条没有本地 PDF 的元数据，保留 ${payload.keptCount ?? 0} 条。`,
      );
    } catch (cleanupError) {
      const message = cleanupError instanceof Error ? cleanupError.message : "清理无 PDF 元数据失败";
      setError(message);
    } finally {
      setIsCleaningMetadata(false);
    }
  }

  /** 把用户输入的本地 PDF 路径关联到论文。 */
  async function linkLocalPdf(paper: PaperResult) {
    const paperKey = paper.id || `${paper.source}-${paper.externalId || paper.title}`;
    const pdfPath = (localPdfPaths[paperKey] || "").trim();
    if (!pdfPath) {
      setError("请先输入本地 PDF 文件路径。");
      return;
    }

    setLinkingPaperId(paperKey);
    setError("");

    try {
      const response = await fetch(buildApiUrl("/api/papers/link-local-pdf"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          pdf_path: pdfPath,
          record_id: paper.id || null,
          doi: paper.doi || null,
          title: paper.title || null,
        }),
      });
      const payload = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(payload.detail || "绑定本地 PDF 失败");
      }

      const updatedPaper = payload.paper as PaperResult;
      setResults((current) =>
        current.map((item) =>
          (item.id && item.id === updatedPaper.id) ||
          (!item.id && item.title === paper.title && item.source === paper.source)
            ? {
                ...item,
                ...updatedPaper,
                pdfPath: updatedPaper.pdfPath || pdfPath,
                requiresManualDownload: false,
                manualDownloadReason: "",
              }
            : item,
        ),
      );
      setSummary(`已绑定本地 PDF：${pdfPath}`);
    } catch (linkError) {
      const message = linkError instanceof Error ? linkError.message : "绑定本地 PDF 失败";
      setError(message);
    } finally {
      setLinkingPaperId("");
    }
  }

  /** 校验检索条件并汇总各来源的流式结果。 */
  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (isSearching) {
      return;
    }

    const keyword = query.trim();
    if (!keyword) {
      setError("请输入文献搜索关键词。");
      setSummary("");
      setResults([]);
      setProgress(
        selectedSources.map((source) => ({
          source,
          state: "error",
          message: "缺少搜索关键词",
          count: 0,
        })),
      );
      return;
    }

    const safeLimit = Math.max(1, Math.min(limitPerSource || 1, 200));
    setIsSearching(true);
    setError("");
    setSummary("已提交到后端 HunterAgent，正在流式接收搜索、筛选、下载和保存状态。");
    setResults([]);
    setAgentLogs(["前端已提交请求，等待后端 HunterAgent 返回状态。"]);
    setProgress(
      selectedSources.map((source) => ({
        source,
        state: "searching",
        message: `HunterAgent 正在处理，每个数据源目标 ${safeLimit} 篇`,
        count: 0,
        logs: [],
      })),
    );

    try {
      const payload = await streamDataset(keyword, safeLimit, (streamEvent) => {
        if (streamEvent.type !== "log") {
          return;
        }

        const log = streamEvent.message;
        setAgentLogs((current) => [...current, log]);
        const source = getSourceFromLog(log);
        if (!source) {
          return;
        }

        updateProgress(source, {
          state: "searching",
          message: log,
        });
        setProgress((current) =>
          current.map((item) =>
            item.source === source
              ? {
                  ...item,
                  logs: [...(item.logs ?? []), log].slice(-4),
                }
              : item,
          ),
        );
      });
      const sourceCounts = new Map<PaperSource, number>();
      const sourceErrors = new Map<PaperSource, string>();

      payload.papers.forEach((paper) => {
        const source = paper.source as PaperSource;
        sourceCounts.set(source, (sourceCounts.get(source) ?? 0) + 1);
      });
      payload.errors.forEach((item) => {
        sourceErrors.set(item.source, item.message);
      });

      selectedSources.forEach((source) => {
        const sourceError = sourceErrors.get(source);
        const sourceLogs = getLogsForSource(payload.logs ?? [], source);

        if (sourceError) {
          updateProgress(source, {
            state: "error",
            message: sourceError,
            count: 0,
            logs: sourceLogs,
          });
          return;
        }

        const count = payload.savedCountsBySource?.[source] ?? sourceCounts.get(source) ?? 0;
        updateProgress(source, {
          state: "done",
          message: `${getSourceLabel(source)} 已完成筛选、下载和保存`,
          count,
          logs: sourceLogs,
        });
      });

      setResults(payload.papers);
      setAgentLogs(payload.logs ?? []);
      setSummary(
        `后端处理完成：检索 ${payload.searchedCount} 篇，去重后 ${payload.deduplicatedCount} 篇，筛选后 ${payload.filteredCount} 篇，总保存 ${payload.savedCount} 篇；每个数据源目标 ${payload.targetPerSource ?? safeLimit} 篇。优先保存已下载 PDF 的论文；若达到最大轮次仍不足，会返回仅元数据结果并提示手动下载。`,
      );
      setCompletionNoticeMessage(
        activeViewRef.current
          ? `下载任务已完成，共保存 ${payload.savedCount} 篇结果。`
          : `下载任务已完成，共保存 ${payload.savedCount} 篇结果，可返回“下载数据集”查看详情。`,
      );
      setCompletionNoticeOpen(true);
      setError(
        payload.errors.length > 0
          ? payload.errors.map((item) => `${getSourceLabel(item.source)}：${item.message}`).join("；")
          : "",
      );
    } catch (downloadError) {
      const message =
        downloadError instanceof Error ? downloadError.message : "HunterAgent 下载数据集失败";
      setError(message);
      setSummary("后端请求失败，请查看后端终端输出。");
      setAgentLogs((current) => [...current, `请求失败：${message}`]);
      setProgress(
        selectedSources.map((source) => ({
          source,
          state: "error",
          message,
          count: 0,
        })),
      );
    } finally {
      setIsSearching(false);
    }
  }

  return (
    <Box
      component="main"
      sx={{
        minHeight: embedded ? "calc(100vh - 64px)" : "100vh",
        position: "relative",
        overflow: "hidden",
        background: `
          radial-gradient(circle at 18% 24%, rgba(42, 92, 170, 0.24), transparent 34%),
          radial-gradient(circle at 82% 8%, rgba(139, 92, 246, 0.2), transparent 30%),
          radial-gradient(circle at 50% 95%, rgba(14, 165, 233, 0.1), transparent 34%),
          linear-gradient(135deg, #05070c 0%, #09111d 46%, #10081c 100%)
        `,
      }}
    >
      <Box
        aria-hidden="true"
        sx={{
          position: "absolute",
          width: 180,
          height: 180,
          left: "8%",
          top: "18%",
          borderRadius: 999,
          background: "rgba(42, 92, 170, 0.1)",
          filter: "blur(2px)",
          pointerEvents: "none",
        }}
      />
      <Box
        aria-hidden="true"
        sx={{
          position: "absolute",
          width: 240,
          height: 240,
          right: "10%",
          top: "34%",
          borderRadius: 999,
          background: "rgba(139, 92, 246, 0.1)",
          filter: "blur(2px)",
          pointerEvents: "none",
        }}
      />

      <Container maxWidth="lg" sx={{ position: "relative", zIndex: 1, pt: "32px", pb: { xs: 4, md: 7 } }}>
        <Stack spacing={4}>
          <Paper
            component="form"
            onSubmit={handleSearch}
            elevation={0}
            sx={{
              ...glassPanel,
              borderRadius: 2,
              p: { xs: 2.25, md: 3 },
            }}
          >
            <Typography sx={{ color: "#f8fafc", fontSize: "1.25rem", fontWeight: 900, mb: 2 }}>
              论文搜索
            </Typography>

            <TextField
              label="搜索关键词"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="large language model"
              fullWidth
              sx={darkTextFieldSx}
              slotProps={{
                input: {
                  startAdornment: (
                    <SearchIcon sx={{ mr: 1, color: "rgba(226, 232, 240, 0.72)" }} />
                  ),
                },
              }}
            />

            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: {
                  xs: "1fr",
                  sm: "repeat(2, minmax(0, 1fr))",
                  md: "1.15fr 1fr 1fr",
                },
                gap: 1.5,
                mt: 2,
              }}
            >
              <TextField
                label="每源目标论文数"
                type="number"
                value={limitPerSource}
                onChange={(event) => setLimitPerSource(Number(event.target.value))}
                sx={darkTextFieldSx}
                slotProps={{
                  htmlInput: {
                    min: 1,
                    max: 200,
                    style: { textAlign: "center", fontWeight: 800 },
                  },
                }}
              />
              <TextField
                label="起始年份"
                type="number"
                value={yearFrom}
                onChange={(event) => setYearFrom(event.target.value)}
                sx={darkTextFieldSx}
                slotProps={{ htmlInput: { min: 1900, max: 2100 } }}
              />
              <TextField
                label="结束年份"
                type="number"
                value={yearTo}
                onChange={(event) => setYearTo(event.target.value)}
                sx={darkTextFieldSx}
                slotProps={{ htmlInput: { min: 1900, max: 2100 } }}
              />
            </Box>

            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: { xs: "1fr", md: "120px minmax(0, 1fr)" },
                gap: 1.5,
                mt: 2.25,
                alignItems: "center",
              }}
            >
              <Typography sx={{ color: "rgba(226, 232, 240, 0.72)", fontWeight: 800 }}>
                数据源
              </Typography>
              <Stack direction="row" sx={{ flexWrap: "wrap", gap: 1.25 }}>
                {paperSources.map((item) => {
                  const active = selectedSources.includes(item.id);

                  return (
                    <Chip
                      key={item.id}
                      label={item.label}
                      clickable
                      color={active ? "primary" : "default"}
                      variant={active ? "filled" : "outlined"}
                      onClick={() => toggleSource(item.id)}
                      sx={{
                        borderRadius: 2,
                        height: 44,
                        minWidth: 116,
                        fontWeight: 800,
                        px: 0.75,
                        color: active ? "#ffffff" : "#dbeafe",
                        borderColor: active ? "transparent" : "rgba(147, 197, 253, 0.28)",
                        background: active ? undefined : "rgba(15, 23, 42, 0.5)",
                        ...(active && {
                          background: "linear-gradient(90deg, #2a5caa 0%, #6d5df6 100%)",
                          boxShadow: "0 10px 28px rgba(37, 99, 235, 0.22)",
                        }),
                      }}
                    />
                  );
                })}
              </Stack>
            </Box>

            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: { xs: "1fr", md: "120px minmax(0, 1fr)" },
                gap: 1.5,
                mt: 2,
                alignItems: "center",
              }}
            >
              <Typography sx={{ color: "rgba(226, 232, 240, 0.72)", fontWeight: 800 }}>
                CCF 等级
              </Typography>
              <Stack direction="row" sx={{ flexWrap: "wrap", gap: 1 }}>
                {[
                  { value: "A", label: "A" },
                  { value: "B", label: "B" },
                  { value: "C", label: "C" },
                  { value: "NON_CCF", label: "非 CCF" },
                ].map((item) => {
                  const level = item.value;
                  const active = selectedCcfLevels.includes(level);

                  return (
                    <Chip
                      key={level}
                      label={item.label}
                      clickable
                      variant={active ? "filled" : "outlined"}
                      onClick={() => toggleCcfLevel(level)}
                      sx={{
                        height: 40,
                        minWidth: 78,
                        borderRadius: 2,
                        color: active ? "#ffffff" : "#dbeafe",
                        borderColor: active ? "transparent" : "rgba(147, 197, 253, 0.28)",
                        background: active
                          ? "linear-gradient(90deg, #2a5caa 0%, #6d5df6 100%)"
                          : "rgba(15, 23, 42, 0.5)",
                        fontWeight: 800,
                      }}
                    />
                  );
                })}
              </Stack>
            </Box>

            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: { xs: "1fr", md: "120px minmax(0, 1fr) auto" },
                gap: 1.5,
                mt: 2,
                alignItems: "center",
              }}
            >
              <Typography sx={{ color: "rgba(226, 232, 240, 0.72)", fontWeight: 800 }}>
                最低影响因子
              </Typography>
              <Slider
                value={minImpactFactor}
                min={0}
                max={20}
                step={0.1}
                onChange={(_, value) => setMinImpactFactor(Array.isArray(value) ? value[0] : value)}
                valueLabelDisplay="auto"
                sx={{
                  color: "#60a5fa",
                  "& .MuiSlider-rail": { color: "rgba(148, 163, 184, 0.36)" },
                }}
              />
              <Typography sx={{ color: "#f8fafc", fontWeight: 900, minWidth: 48 }}>
                {minImpactFactor.toFixed(1)}
              </Typography>
            </Box>

            <Stack direction="row" sx={{ mt: 2.25, gap: 1.25, flexWrap: "wrap" }}>
              <Button
                type="submit"
                variant="contained"
                disabled={isSearching}
                startIcon={isSearching ? <CircularProgress size={16} /> : <SearchIcon />}
                sx={{
                  minWidth: 118,
                  height: 42,
                  borderRadius: 2,
                  fontWeight: 900,
                  background: "linear-gradient(90deg, #2a5caa 0%, #6d5df6 100%)",
                }}
              >
                搜索
              </Button>
              <Button
                type="button"
                variant="outlined"
                disabled={isSearching}
                startIcon={<RestartAltIcon />}
                onClick={handleReset}
                sx={{
                  minWidth: 104,
                  height: 42,
                  borderRadius: 2,
                  fontWeight: 900,
                  borderColor: "rgba(147, 197, 253, 0.28)",
                  color: "#dbeafe",
                }}
              >
                重置
              </Button>
              <Button
                type="button"
                variant="outlined"
                disabled={isSearching || isCleaningMetadata}
                onClick={cleanupMissingPdfs}
                sx={{
                  minWidth: 168,
                  height: 42,
                  borderRadius: 2,
                  fontWeight: 900,
                  borderColor: "rgba(251, 191, 36, 0.34)",
                  color: "#fde68a",
                }}
              >
                {isCleaningMetadata ? "清理中" : "清理无 PDF 元数据"}
              </Button>
            </Stack>

            <Typography sx={{ mt: 1.5, color: "rgba(203, 213, 225, 0.84)", lineHeight: 1.7 }}>
              当前数据源：{selectedSourceText || "未选择"}。点击搜索后，HunterAgent 会优先检索已出版来源
              {selectedPublishedSources.length > 0 ? `（${selectedPublishedSources.map(getSourceLabel).join(" / ")}）` : ""}
              并应用 CCF/影响因子条件。
              {selectedArxiv ? (
                <Box component="span" sx={{ display: "block", mt: 0.5, color: "#fde68a" }}>
                  arXiv 为预印本数据源，不代表已发表期刊或会议；后端不会用 CCF 等级和影响因子过滤 arXiv。
                </Box>
              ) : null}
            </Typography>
          </Paper>

          <Box>
            <Box
              sx={{
                display: "flex",
                flexDirection: { xs: "column", sm: "row" },
                justifyContent: "space-between",
                alignItems: { xs: "flex-start", sm: "flex-end" },
                gap: 2,
                mb: 2,
              }}
            >
              <Box>
                <Typography
                  variant="h5"
                  component="h2"
                  sx={{ mt: 0.5, color:  "#60a5fa", fontWeight: 800 }}
                >
                  搜索进展与结果
                </Typography>
              </Box>
              <Chip
                label={isSearching ? "搜索中" : `${results.length} 条结果`}
                color={isSearching ? "success" : "primary"}
                variant="outlined"
                sx={{
                  borderColor: "rgba(96, 165, 250, 0.42)",
                  background: "rgba(37, 99, 235, 0.1)",
                  color: "#60a5fa",
                  fontWeight: 800,
                }}
              />
            </Box>

            <Paper
              elevation={0}
              sx={{
                ...glassPanel,
                minHeight: 300,
                borderRadius: 2,
                p: { xs: 2, md: 3.5 },
              }}
            >
              {error && (
                <Alert
                  severity="error"
                  icon={<ErrorOutlinedIcon fontSize="large" />}
                  sx={{
                    mb: 2.5,
                    border: "1px solid rgba(251, 113, 133, 0.46)",
                    borderRadius: 2,
                    background: "rgba(127, 29, 29, 0.34)",
                    color: "#fecdd3",
                    "& .MuiAlert-message": {
                      fontFamily: '"Courier New", Consolas, monospace',
                      fontSize: "1rem",
                    },
                  }}
                >
                  搜索失败：{error}
                </Alert>
              )}
              {summary && (
                <Alert
                  severity={error ? "warning" : "info"}
                  sx={{
                    mb: 2.5,
                    border: "1px solid rgba(96, 165, 250, 0.32)",
                    borderRadius: 2,
                    background: "rgba(30, 64, 175, 0.18)",
                    color: "#bfdbfe",
                  }}
                >
                  {summary}
                </Alert>
              )}
              {manualDownloadCount > 0 && (
                <Alert
                  severity="warning"
                  sx={{
                    mb: 2.5,
                    border: "1px solid rgba(251, 191, 36, 0.34)",
                    borderRadius: 2,
                    background: "rgba(120, 53, 15, 0.24)",
                    color: "#fde68a",
                  }}
                >
                  有 {manualDownloadCount} 篇结果当前只有元数据或自动 PDF 下载失败。请点击“查看原文”或“PDF
                  链接”手动下载到本地，后续才能解析论文正文。
                </Alert>
              )}

              <Box
                sx={{
                  display: "grid",
                  gridTemplateColumns: {
                    xs: "1fr",
                    sm: "repeat(2, minmax(0, 1fr))",
                    md: `repeat(${Math.min(selectedProgress.length, 4)}, minmax(0, 1fr))`,
                  },
                  gap: 1.5,
                }}
              >
                {selectedProgress.map((item) => {
                  const visual = getProgressVisual(item.state);

                  return (
                    <Paper
                      key={item.source}
                      elevation={0}
                      sx={{
                        display: "flex",
                        justifyContent: "space-between",
                        gap: 1.5,
                        minHeight: 112,
                        border: `1px solid ${visual.border}`,
                        borderRadius: 2,
                        background: visual.bg,
                        backdropFilter: "blur(12px)",
                        color: visual.color,
                        p: 2,
                      }}
                    >
                      <Box>
                        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                          {visual.icon}
                          <Typography sx={{ fontWeight: 800 }}>
                            {getSourceLabel(item.source)}
                          </Typography>
                        </Box>
                        <Typography sx={{ mt: 1, color: "inherit", lineHeight: 1.55 }}>
                          {item.message}
                        </Typography>
                        {item.logs && item.logs.length > 0 && (
                          <Stack spacing={0.5} sx={{ mt: 1.25 }}>
                            {item.logs.map((log, index) => (
                              <Typography
                                key={`${item.source}-${index}-${log}`}
                                sx={{
                                  color: "rgba(226, 232, 240, 0.82)",
                                  fontFamily: '"Courier New", Consolas, monospace',
                                  fontSize: "0.78rem",
                                  lineHeight: 1.45,
                                  wordBreak: "break-word",
                                }}
                              >
                                {log}
                              </Typography>
                            ))}
                          </Stack>
                        )}
                      </Box>
                      <Typography sx={{ fontSize: "1.4rem", fontWeight: 900 }}>
                        {item.count}
                      </Typography>
                    </Paper>
                  );
                })}
              </Box>

              <Divider sx={{ my: 2.5, borderColor: "rgba(148, 163, 184, 0.18)" }} />

              {agentLogs.length > 0 && (
                <Box
                  sx={{
                    mb: 2.5,
                    border: "1px solid rgba(148, 163, 184, 0.18)",
                    borderRadius: 2,
                    background: "rgba(2, 6, 23, 0.42)",
                    p: 2,
                  }}
                >
                  <Typography sx={{ color: "#f8fafc", fontWeight: 800, mb: 1 }}>
                    HunterAgent 状态
                  </Typography>
                  <Stack spacing={0.75}>
                    {agentLogs.map((log, index) => (
                      <Typography
                        key={`${index}-${log}`}
                        sx={{
                          color: "rgba(203, 213, 225, 0.86)",
                          fontFamily: '"Courier New", Consolas, monospace',
                          fontSize: "0.9rem",
                          lineHeight: 1.55,
                        }}
                      >
                        {index + 1}. {log}
                      </Typography>
                    ))}
                  </Stack>
                </Box>
              )}

              {results.length === 0 && !error ? (
                <Box
                  sx={{
                    border: "1px dashed rgba(147, 197, 253, 0.28)",
                    borderRadius: 2,
                    background: "rgba(15, 23, 42, 0.46)",
                    color: "rgba(203, 213, 225, 0.82)",
                    textAlign: "center",
                    p: 3.5,
                  }}
                >
                  <Typography sx={{ color: "#f8fafc", fontWeight: 800 }}>
                    {agentLogs.length > 1 ? "没有符合筛选条件的结果" : "等待搜索结果"}
                  </Typography>
                  <Typography sx={{ mt: 1 }}>
                    {agentLogs.length > 1
                      ? "请查看上方 HunterAgent 状态中的过滤原因，或放宽年份、SJR/影响因子代理指标、CCF 条件。"
                      : "输入关键词并点击搜索后，所选数据源的搜索进展和论文结果会显示在这里。"}
                  </Typography>
                </Box>
              ) : (
                <Stack spacing={1.75}>
                  {results.map((paper, index) => (
                    <Paper
                      key={`${paper.source}-${paper.externalId || index}-${paper.title}`}
                      elevation={0}
                      sx={{
                        border: "1px solid rgba(148, 163, 184, 0.18)",
                        borderRadius: 2,
                        background: "rgba(15, 23, 42, 0.62)",
                        backdropFilter: "blur(12px)",
                        p: 2.25,
                      }}
                    >
                      {(() => {
                        const paperKey = paper.id || `${paper.source}-${paper.externalId || paper.title}`;
                        const needsManualPdf = paper.requiresManualDownload || !paper.pdfPath;

                        return (
                          <>
                      <Stack direction="row" sx={{ mb: 1, flexWrap: "wrap", gap: 1 }}>
                        <Chip
                          size="small"
                          label={paper.source}
                          variant="outlined"
                          sx={{ borderColor: "rgba(96, 165, 250, 0.42)", color: "#93c5fd" }}
                        />
                        {paper.year && (
                          <Chip
                            size="small"
                            label={paper.year}
                            variant="outlined"
                            sx={{ borderColor: "rgba(148, 163, 184, 0.28)", color: "#cbd5e1" }}
                          />
                        )}
                        {paper.venue && (
                          <Chip
                            size="small"
                            label={paper.venue}
                            variant="outlined"
                            sx={{ borderColor: "rgba(148, 163, 184, 0.28)", color: "#cbd5e1" }}
                          />
                        )}
                        {!paper.metricFiltersIgnored &&
                          paper.impactFactor !== undefined &&
                          paper.impactFactor !== null && (
                          <Chip
                            size="small"
                            label={`IF ${paper.impactFactor}`}
                            variant="outlined"
                            sx={{ borderColor: "rgba(52, 211, 153, 0.38)", color: "#a7f3d0" }}
                          />
                        )}
                        {!paper.metricFiltersIgnored && paper.ccfLevel && (
                          <Chip
                            size="small"
                            label={`CCF ${paper.ccfLevel}`}
                            variant="outlined"
                            sx={{ borderColor: "rgba(167, 139, 250, 0.42)", color: "#ddd6fe" }}
                          />
                        )}
                        {paper.metricFiltersIgnored && (
                          <Chip
                            size="small"
                            label="预印本：CCF/IF 不适用"
                            variant="outlined"
                            sx={{ borderColor: "rgba(251, 191, 36, 0.42)", color: "#fde68a" }}
                          />
                        )}
                        {paper.pdfPath ? (
                          <Chip
                            size="small"
                            label="PDF 已下载"
                            variant="outlined"
                            sx={{ borderColor: "rgba(52, 211, 153, 0.38)", color: "#a7f3d0" }}
                          />
                        ) : (
                          <Chip
                            size="small"
                            label="需手动下载"
                            variant="outlined"
                            sx={{ borderColor: "rgba(251, 191, 36, 0.42)", color: "#fde68a" }}
                          />
                        )}
                      </Stack>
                      <Typography
                        component="h3"
                        sx={{ color: "#f8fafc", fontWeight: 800, lineHeight: 1.45 }}
                      >
                        {paper.title}
                      </Typography>
                      {paper.authors && paper.authors.length > 0 && (
                        <Typography
                          sx={{ mt: 1, color: "rgba(203, 213, 225, 0.82)", lineHeight: 1.7 }}
                        >
                          {paper.authors.slice(0, 4).join(", ")}
                        </Typography>
                      )}
                      {paper.abstract && (
                        <Typography
                          sx={{
                            mt: 1,
                            color: "rgba(203, 213, 225, 0.82)",
                            lineHeight: 1.7,
                            display: "-webkit-box",
                            WebkitLineClamp: 3,
                            WebkitBoxOrient: "vertical",
                            overflow: "hidden",
                          }}
                        >
                          {paper.abstract}
                        </Typography>
                      )}
                      {needsManualPdf && (
                        <Alert
                          severity="warning"
                          sx={{
                            mt: 1.5,
                            border: "1px solid rgba(251, 191, 36, 0.28)",
                            borderRadius: 2,
                            background: "rgba(120, 53, 15, 0.18)",
                            color: "#fde68a",
                          }}
                        >
                          {paper.manualDownloadReason ||
                            "该结果已保存元数据，但还没有本地 PDF。请手动下载论文 PDF 后再进行正文解析。"}
                        </Alert>
                      )}
                      {needsManualPdf && (
                        <Box
                          sx={{
                            display: "grid",
                            gridTemplateColumns: { xs: "1fr", md: "minmax(0, 1fr) auto" },
                            gap: 1.25,
                            mt: 1.5,
                          }}
                        >
                          <TextField
                            label="本地 PDF 路径"
                            value={localPdfPaths[paperKey] || ""}
                            onChange={(event) =>
                              setLocalPdfPaths((current) => ({
                                ...current,
                                [paperKey]: event.target.value,
                              }))
                            }
                            placeholder="例如 E:\\research_agent\\backend\\storage\\papers\\xxx.pdf"
                            fullWidth
                            sx={darkTextFieldSx}
                          />
                          <Button
                            type="button"
                            variant="outlined"
                            disabled={linkingPaperId === paperKey}
                            onClick={() => linkLocalPdf(paper)}
                            sx={{
                              minWidth: 120,
                              borderRadius: 2,
                              fontWeight: 900,
                              borderColor: "rgba(251, 191, 36, 0.42)",
                              color: "#fde68a",
                            }}
                          >
                            {linkingPaperId === paperKey ? "绑定中" : "绑定 PDF"}
                          </Button>
                        </Box>
                      )}
                      <Stack direction="row" spacing={2} sx={{ mt: 1.5 }}>
                        {paper.url && (
                          <Box
                            component="a"
                            href={paper.url}
                            target="_blank"
                            rel="noreferrer"
                            sx={{ color: "#60a5fa", fontWeight: 800 }}
                          >
                            查看原文
                          </Box>
                        )}
                        {paper.pdfUrl && (
                          <Box
                            component="a"
                            href={paper.pdfUrl}
                            target="_blank"
                            rel="noreferrer"
                            sx={{ color: "#60a5fa", fontWeight: 800 }}
                          >
                            {paper.pdfPath ? "PDF" : "PDF 链接"}
                          </Box>
                        )}
                      </Stack>
                          </>
                        );
                      })()}
                    </Paper>
                  ))}
                </Stack>
              )}
            </Paper>
          </Box>
        </Stack>
      </Container>
      <Snackbar
        open={completionNoticeOpen}
        autoHideDuration={5000}
        onClose={(_, reason) => {
          if (reason === "clickaway") {
            return;
          }
          setCompletionNoticeOpen(false);
        }}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
      >
        <Alert
          onClose={() => setCompletionNoticeOpen(false)}
          severity="success"
          variant="filled"
          sx={{
            alignItems: "center",
            minWidth: 320,
            boxShadow: "0 18px 45px rgba(15, 23, 42, 0.35)",
          }}
        >
          {completionNoticeMessage}
        </Alert>
      </Snackbar>
    </Box>
  );
}
