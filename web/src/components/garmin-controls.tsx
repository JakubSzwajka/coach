"use client";

import { RefreshCw, Unplug, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface JobStatus {
  state: "running" | "succeeded" | "failed";
  action: "connect" | "refresh";
  step: string;
  error?: string;
  progress?: { current: number; total: number };
}

interface GarminStatus {
  connected: boolean;
  job: JobStatus | null;
}

const ERROR_LABELS: Record<string, string> = {
  collection_degraded: "Garmin returned incomplete data. Try again shortly.",
  collection_failed: "Garmin collection failed. Check the credentials and try again.",
  invalid_request: "The connection details were rejected.",
  job_stale: "The previous job stopped responding. Start it again.",
  job_unavailable: "Could not start the Garmin update.",
  not_connected: "Connect Garmin before refreshing.",
  worker_exited: "The background collector stopped. Start the backfill again.",
};
function runningLabel(job: JobStatus): string {
  if (job.step === "securing_credentials") return "Encrypting credentials…";
  if (job.step === "authenticating") return "Signing in to Garmin…";
  if (job.step === "daily" && job.progress) {
    const prefix = job.action === "connect" ? "Backfilling" : "Refreshing";
    return `${prefix} day ${job.progress.current} / ${job.progress.total}`;
  }
  if (job.step === "plans") return "Updating plans…";
  if (job.step === "activities") return "Updating activities…";
  if (job.step === "profile") return "Updating athlete profile…";
  if (job.step === "finalizing") return "Finalizing…";
  return job.action === "connect" ? "Connecting…" : "Refreshing…";
}

export function GarminControls() {
  const router = useRouter();
  const [status, setStatus] = useState<GarminStatus | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [days, setDays] = useState(365);
  const [formError, setFormError] = useState<string | null>(null);
  const lastJobState = useRef<string | null>(null);

  const loadStatus = useCallback(async () => {
    const response = await fetch("/api/garmin/status", { cache: "no-store" });
    if (!response.ok) return;
    const next = (await response.json()) as GarminStatus;
    setStatus(next);
    if (lastJobState.current === "running" && next.job?.state === "succeeded") {
      router.refresh();
    }
    lastJobState.current = next.job?.state ?? null;
  }, [router]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (status?.job?.state !== "running") return;
    const timer = window.setInterval(() => void loadStatus(), 1500);
    return () => window.clearInterval(timer);
  }, [loadStatus, status?.job?.state]);

  useEffect(() => {
    if (!modalOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setModalOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [modalOpen]);

  const startJob = async (endpoint: string, body?: object) => {
    setFormError(null);
    const action = endpoint.endsWith("connect") ? "connect" : "refresh";
    const response = await fetch(endpoint, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      const result = (await response.json().catch(() => ({}))) as { error?: string };
      const error = result.error ?? "job_unavailable";
      const message =
        error === "job_running"
          ? "A Garmin update is already running."
          : (ERROR_LABELS[error] ?? "Could not start the Garmin update.");
      setFormError(message);
      if (error === "job_running") {
        await loadStatus();
      } else if (action === "refresh") {
        setStatus((current) => ({
          connected: current?.connected ?? false,
          job: { state: "failed", action, step: "failed", error },
        }));
      }
      return false;
    }
    lastJobState.current = "running";
    setStatus((current) => ({
      connected: current?.connected ?? false,
      job: { state: "running", action, step: "starting" },
    }));
    return true;
  };

  const connect = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const started = await startJob("/api/garmin/connect", { email, password, days });
    setPassword("");
    if (started) setModalOpen(false);
  };

  const job = status?.job;
  const running = job?.state === "running";
  const statusLabel =
    job?.state === "running"
      ? runningLabel(job)
      : job?.state === "failed"
        ? (ERROR_LABELS[job.error ?? ""] ?? "Garmin update failed.")
        : null;
  const compactProgress =
    running && job?.step === "daily" && job.progress
      ? `${job.progress.current}/${job.progress.total}`
      : null;

  return (
    <>
      <div className="flex items-center gap-2">
        {statusLabel ? (
          <span className="text-muted-foreground hidden max-w-52 truncate text-xs lg:inline">
            {statusLabel}
          </span>
        ) : null}
        {status?.connected ? (
          <Button
            variant="outline"
            size="sm"
            disabled={running}
            onClick={() => void startJob("/api/garmin/refresh")}
          >
            <RefreshCw className={running ? "animate-spin" : ""} />
            {running ? (compactProgress ?? "Updating") : "Refresh Garmin"}
          </Button>
        ) : (
          <Button size="sm" disabled={running} onClick={() => setModalOpen(true)}>
            {running ? <RefreshCw className="animate-spin" /> : <Unplug />}
            {running ? (compactProgress ?? "Connecting") : "Connect Garmin"}
          </Button>
        )}
      </div>

      {modalOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4">
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="garmin-connect-title"
            className="bg-background w-full max-w-md rounded-xl border p-6 shadow-xl"
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 id="garmin-connect-title" className="text-lg font-semibold">
                  Connect Garmin
                </h2>
                <p className="text-muted-foreground mt-1 text-sm">
                  Credentials go directly to the server, are encrypted before storage, and are never
                  stored by the browser. Configure an external secret key to keep key material
                  outside the data volume.
                </p>
              </div>
              <Button variant="ghost" size="icon" onClick={() => setModalOpen(false)}>
                <X />
                <span className="sr-only">Close</span>
              </Button>
            </div>
            <form className="mt-6 space-y-4" onSubmit={connect}>
              <div className="space-y-2">
                <Label htmlFor="garmin-email">Garmin email</Label>
                <Input
                  id="garmin-email"
                  type="email"
                  autoComplete="username"
                  required
                  maxLength={320}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="garmin-password">Garmin password</Label>
                <Input
                  id="garmin-password"
                  type="password"
                  autoComplete="current-password"
                  required
                  maxLength={512}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="backfill-days">Initial history</Label>
                <Input
                  id="backfill-days"
                  type="number"
                  min={1}
                  max={730}
                  required
                  value={days}
                  onChange={(event) => setDays(Number(event.target.value))}
                />
                <p className="text-muted-foreground text-xs">
                  Days to collect on first connect (1–730).
                </p>
              </div>
              {formError ? <p className="text-destructive text-sm">{formError}</p> : null}
              <div className="flex justify-end gap-2 pt-2">
                <Button type="button" variant="outline" onClick={() => setModalOpen(false)}>
                  Cancel
                </Button>
                <Button type="submit">Connect and backfill</Button>
              </div>
            </form>
          </section>
        </div>
      ) : null}
    </>
  );
}
