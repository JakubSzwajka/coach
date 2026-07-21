import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { access, readFile } from "node:fs/promises";
import path from "node:path";
import { dataRoot } from "./coach-data";
import { boundProfileRootForSubject } from "./profile";

export type GarminJobAction = "connect" | "refresh";
export type GarminJobState = "running" | "succeeded" | "failed";

export interface GarminJobStatus {
  state: GarminJobState;
  action: GarminJobAction;
  step: string;
  error?: string;
  started_at?: string;
  updated_at?: string;
  finished_at?: string;
  progress?: { current: number; total: number };
}

export interface GarminStatusResponse {
  connected: boolean;
  job: GarminJobStatus | null;
}

export class GarminJobConflict extends Error {}
export class GarminNotConnected extends Error {}

const STALE_JOB_MS = 6 * 60 * 60 * 1000;

function jobKey(subject: string): string {
  return createHash("sha256").update(subject).digest("hex").slice(0, 32);
}

function jobStatusPath(subject: string): string {
  return path.join(/* turbopackIgnore: true */ dataRoot(), "jobs", `${jobKey(subject)}.json`);
}

async function readJobStatus(subject: string): Promise<GarminJobStatus | null> {
  let raw: unknown;
  try {
    raw = JSON.parse(await readFile(jobStatusPath(subject), "utf8"));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw new Error("Garmin job status is unreadable", { cause: error });
  }
  if (!raw || typeof raw !== "object") {
    throw new Error("Garmin job status has an unsupported shape");
  }
  const status = raw as Partial<GarminJobStatus & { worker_pid: number }>;
  if (
    !["running", "succeeded", "failed"].includes(status.state ?? "") ||
    !["connect", "refresh"].includes(status.action ?? "") ||
    typeof status.step !== "string"
  ) {
    throw new Error("Garmin job status has an unsupported shape");
  }
  let progress: GarminJobStatus["progress"];
  if (status.progress !== undefined) {
    const candidate = status.progress as Partial<{ current: number; total: number }>;
    if (
      !candidate ||
      !Number.isInteger(candidate.current) ||
      !Number.isInteger(candidate.total) ||
      Number(candidate.current) < 0 ||
      Number(candidate.total) < 1 ||
      Number(candidate.current) > Number(candidate.total)
    ) {
      throw new Error("Garmin job status has an unsupported progress shape");
    }
    progress = { current: Number(candidate.current), total: Number(candidate.total) };
  }
  const normalized: GarminJobStatus = {
    state: status.state as GarminJobState,
    action: status.action as GarminJobAction,
    step: status.step,
    ...(typeof status.error === "string" ? { error: status.error } : {}),
    ...(typeof status.started_at === "string" ? { started_at: status.started_at } : {}),
    ...(typeof status.updated_at === "string" ? { updated_at: status.updated_at } : {}),
    ...(typeof status.finished_at === "string" ? { finished_at: status.finished_at } : {}),
    ...(progress ? { progress } : {}),
  };
  if (normalized.state === "running") {
    if (!Number.isInteger(status.worker_pid)) {
      return { ...normalized, state: "failed", step: "failed", error: "worker_exited" };
    }
    try {
      process.kill(Number(status.worker_pid), 0);
    } catch {
      return { ...normalized, state: "failed", step: "failed", error: "worker_exited" };
    }
  }
  if (
    normalized.state === "running" &&
    normalized.updated_at &&
    Date.now() - Date.parse(normalized.updated_at) > STALE_JOB_MS
  ) {
    return { ...normalized, state: "failed", step: "failed", error: "job_stale" };
  }
  return normalized;
}

async function hasStoredCredentials(subject: string): Promise<boolean> {
  const root = await boundProfileRootForSubject(subject);
  if (!root) return false;
  try {
    await access(
      path.join(/* turbopackIgnore: true */ root, "secrets", "garmin.enc"),
      constants.R_OK,
    );
    return true;
  } catch {
    return false;
  }
}

export async function getGarminStatus(subject: string): Promise<GarminStatusResponse> {
  const [storedCredentials, job] = await Promise.all([
    hasStoredCredentials(subject),
    readJobStatus(subject),
  ]);
  const failedInitialConnection = job?.action === "connect" && job.state === "failed";
  return { connected: storedCredentials && !failedInitialConnection, job };
}

export async function startGarminJob(
  subject: string,
  request:
    | { action: "connect"; email: string; password: string; days: number }
    | { action: "refresh" },
): Promise<void> {
  const current = await readJobStatus(subject);
  if (current?.state === "running") throw new GarminJobConflict();
  if (request.action === "refresh" && !(await hasStoredCredentials(subject))) {
    throw new GarminNotConnected();
  }

  const repoRoot = process.env.GARMIN_COACH_REPO_ROOT?.trim() || "..";
  const python = process.env.GARMIN_COACH_PYTHON?.trim() || ".venv/bin/python";
  const childEnvironment: NodeJS.ProcessEnv = {
    NODE_ENV: process.env.NODE_ENV,
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    TMPDIR: process.env.TMPDIR,
    LANG: process.env.LANG,
    SSL_CERT_FILE: process.env.SSL_CERT_FILE,
    REQUESTS_CA_BUNDLE: process.env.REQUESTS_CA_BUNDLE,
    GARMIN_COACH_DATA_DIR: dataRoot(),
    GARMIN_COACH_SECRET_KEY: process.env.GARMIN_COACH_SECRET_KEY,
    PYTHONPATH: repoRoot,
    PYTHONUNBUFFERED: "1",
  };
  const child = spawn(/* turbopackIgnore: true */ python, ["-m", "coach.web_job"], {
    cwd: repoRoot,
    detached: true,
    env: childEnvironment,
    stdio: ["pipe", "ignore", "ignore"],
  });
  await new Promise<void>((resolve, reject) => {
    child.once("spawn", resolve);
    child.once("error", reject);
  });
  child.stdin.end(JSON.stringify({ ...request, subject }));
  child.unref();
}
