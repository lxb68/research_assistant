"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Drawer,
  IconButton,
  LinearProgress,
  Snackbar,
  Tooltip,
  Typography,
} from "@mui/material";
import TaskAltRounded from "@mui/icons-material/TaskAltRounded";
import CloseRounded from "@mui/icons-material/CloseRounded";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import StopCircleOutlined from "@mui/icons-material/StopCircleOutlined";
import ReplayRounded from "@mui/icons-material/ReplayRounded";
import { buildApiUrl } from "@/lib/api";
import type { BackgroundJob } from "@/lib/background-jobs";

const RECENT_JOB_IDS_KEY = "research-agent.background-job-ids";
const ACTIVE = new Set(["queued", "running", "cancelling"]);
const TYPE_LABELS: Record<string, string> = {
  dataset_download: "数据集下载",
  research_chat: "研究对话",
  pdf_import: "PDF 导入",
  domain_tree: "领域树与知识图谱",
};

type SubmitOptions = {
  conversationId?: string;
  messageId?: string;
  responseMessageId?: string;
  dedupeKey?: string;
};

type BackgroundTaskContextValue = {
  jobs: BackgroundJob[];
  activeCount: number;
  submitJob: (type: string, payload: Record<string, unknown>, options?: SubmitOptions) => Promise<BackgroundJob>;
  registerJob: (job: BackgroundJob) => void;
  refresh: () => Promise<void>;
  cancelJob: (jobId: string) => Promise<void>;
  retryJob: (jobId: string) => Promise<BackgroundJob>;
  openCenter: () => void;
};

const BackgroundTaskContext = createContext<BackgroundTaskContextValue | null>(null);

async function readError(response: Response, fallback: string) {
  const payload = await response.json().catch(() => ({}));
  return payload.detail || payload.error || fallback;
}

export function BackgroundTaskProvider({ children }: { children: React.ReactNode }) {
  const [jobs, setJobs] = useState<BackgroundJob[]>([]);
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const previousStatuses = useRef(new Map<string, string>());

  const mergeJobs = useCallback((incoming: BackgroundJob[]) => {
    setJobs((current) => {
      const byId = new Map(current.map((job) => [job.jobId, job]));
      for (const job of incoming) byId.set(job.jobId, job);
      return [...byId.values()].sort((left, right) => right.createdAt.localeCompare(left.createdAt));
    });
    try {
      window.localStorage.setItem(RECENT_JOB_IDS_KEY, JSON.stringify(incoming.slice(0, 100).map((job) => job.jobId)));
    } catch {
      // localStorage 只是最近任务索引缓存，失败时仍以服务端为准。
    }
  }, []);

  const refresh = useCallback(async () => {
    const response = await fetch(buildApiUrl("/api/jobs?sessionId=local&limit=100"), { cache: "no-store" });
    if (!response.ok) throw new Error(await readError(response, "读取后台任务列表失败"));
    const payload = (await response.json()) as { jobs?: BackgroundJob[] };
    const nextJobs = payload.jobs ?? [];
    for (const job of nextJobs) {
      const previous = previousStatuses.current.get(job.jobId);
      if (previous && ACTIVE.has(previous) && job.status === "completed") {
        setNotice(`${TYPE_LABELS[job.type] ?? job.type}已完成`);
      }
      previousStatuses.current.set(job.jobId, job.status);
    }
    mergeJobs(nextJobs);
  }, [mergeJobs]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh().catch(() => undefined), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  const activeCount = jobs.filter((job) => ACTIVE.has(job.status)).length;
  useEffect(() => {
    const interval = window.setInterval(() => void refresh().catch(() => undefined), activeCount ? 1500 : 10000);
    return () => window.clearInterval(interval);
  }, [activeCount, refresh]);

  const registerJob = useCallback((job: BackgroundJob) => mergeJobs([job]), [mergeJobs]);

  const submitJob = useCallback(async (
    type: string,
    payload: Record<string, unknown>,
    options: SubmitOptions = {},
  ) => {
    const response = await fetch(buildApiUrl("/api/jobs"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, payload, sessionId: "local", ...options }),
    });
    if (!response.ok) throw new Error(await readError(response, "提交后台任务失败"));
    const job = (await response.json()) as BackgroundJob;
    registerJob(job);
    return job;
  }, [registerJob]);

  const cancelJob = useCallback(async (jobId: string) => {
    const response = await fetch(buildApiUrl(`/api/jobs/${jobId}/cancel`), { method: "POST" });
    if (!response.ok) throw new Error(await readError(response, "取消后台任务失败"));
    registerJob((await response.json()) as BackgroundJob);
  }, [registerJob]);

  const retryJob = useCallback(async (jobId: string) => {
    const response = await fetch(buildApiUrl(`/api/jobs/${jobId}/retry`), { method: "POST" });
    if (!response.ok) throw new Error(await readError(response, "重试后台任务失败"));
    const job = (await response.json()) as BackgroundJob;
    registerJob(job);
    return job;
  }, [registerJob]);

  const value = useMemo<BackgroundTaskContextValue>(() => ({
    jobs,
    activeCount,
    submitJob,
    registerJob,
    refresh,
    cancelJob,
    retryJob,
    openCenter: () => setOpen(true),
  }), [activeCount, cancelJob, jobs, refresh, registerJob, retryJob, submitJob]);

  return (
    <BackgroundTaskContext.Provider value={value}>
      {children}
      <Tooltip title="后台任务中心">
        <Button
          onClick={() => setOpen(true)}
          variant="contained"
          startIcon={activeCount ? <CircularProgress size={16} color="inherit" /> : <TaskAltRounded />}
          sx={{ position: "fixed", right: 24, bottom: 24, zIndex: 1200, borderRadius: 99, boxShadow: 6 }}
        >
          任务{activeCount ? ` ${activeCount}` : ""}
        </Button>
      </Tooltip>
      <Drawer anchor="right" open={open} onClose={() => setOpen(false)}>
        <Box sx={{ width: { xs: "100vw", sm: 430 }, p: 2.5 }}>
          <Box sx={{ display: "flex", alignItems: "center", mb: 2 }}>
            <Box sx={{ flex: 1 }}>
              <Typography variant="h6" sx={{ fontWeight: 800 }}>后台任务中心</Typography>
              <Typography variant="body2" color="text.secondary">页面关闭或切换后任务仍会继续</Typography>
            </Box>
            <IconButton onClick={() => void refresh()} aria-label="刷新任务"><RefreshRounded /></IconButton>
            <IconButton onClick={() => setOpen(false)} aria-label="关闭任务中心"><CloseRounded /></IconButton>
          </Box>
          <Box sx={{ display: "grid", gap: 1.5 }}>
            {jobs.map((job) => (
              <Box key={job.jobId} sx={{ border: 1, borderColor: "divider", borderRadius: 3, p: 1.75 }}>
                <Box sx={{ display: "flex", gap: 1, alignItems: "center" }}>
                  <Typography sx={{ flex: 1, fontWeight: 700 }}>{TYPE_LABELS[job.type] ?? job.type}</Typography>
                  <Chip size="small" label={job.status} color={job.status === "completed" ? "success" : job.status === "failed" ? "error" : "default"} />
                </Box>
                <Typography variant="body2" color="text.secondary" sx={{ my: 1 }}>{job.message || job.stage}</Typography>
                <LinearProgress variant="determinate" value={job.progress || 0} sx={{ borderRadius: 2, height: 7 }} />
                <Box sx={{ mt: 1, display: "flex", alignItems: "center", gap: 1 }}>
                  <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>{job.jobId.slice(0, 12)}</Typography>
                  {ACTIVE.has(job.status) ? (
                    <Button size="small" color="error" startIcon={<StopCircleOutlined />} onClick={() => void cancelJob(job.jobId)}>取消</Button>
                  ) : job.status !== "completed" && job.retryable ? (
                    <Button size="small" startIcon={<ReplayRounded />} onClick={() => void retryJob(job.jobId)}>重试</Button>
                  ) : null}
                </Box>
                {job.error ? <Alert severity="error" sx={{ mt: 1 }}>{job.error}</Alert> : null}
              </Box>
            ))}
            {!jobs.length ? <Alert severity="info">还没有后台任务。</Alert> : null}
          </Box>
        </Box>
      </Drawer>
      <Snackbar open={Boolean(notice)} autoHideDuration={5000} onClose={() => setNotice("")}>
        <Alert severity="success" onClose={() => setNotice("")}>{notice}</Alert>
      </Snackbar>
    </BackgroundTaskContext.Provider>
  );
}

export function useBackgroundTasks() {
  const value = useContext(BackgroundTaskContext);
  if (!value) throw new Error("useBackgroundTasks 必须在 BackgroundTaskProvider 内使用");
  return value;
}
