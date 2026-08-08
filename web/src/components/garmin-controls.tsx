"use client";

import { RefreshCw, Unplug, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface GarminStatus {
  connected: boolean;
  phase:
    | "not_connected"
    | "credentials_stored"
    | "authenticating"
    | "syncing"
    | "first_sync_complete"
    | "connected"
    | "degraded"
    | "degraded_stale"
    | "needs_reconnect";
  safe_code?: string | null;
  job: {
    state: "requested" | "running" | "succeeded" | "failed";
    kind: "initial_sync" | "incremental";
    safe_code?: string | null;
  } | null;
}

const ERROR_LABELS: Record<string, string> = {
  application_unavailable: "The Coach service is unavailable. Try again shortly.",
  collection_failed: "Garmin collection failed. Try again shortly.",
  conflict: "A Garmin update is already running or the connection is unavailable.",
  invalid_request: "The connection details were rejected.",
  rate_limited: "Garmin is rate limiting requests. Try again later.",
};

const PHASE_LABELS: Record<GarminStatus["phase"], string | null> = {
  not_connected: null,
  credentials_stored: "Credentials stored; waiting to authenticate…",
  authenticating: "Authenticating with Garmin…",
  syncing: "Syncing Garmin data…",
  first_sync_complete: "First Garmin sync complete",
  connected: null,
  degraded: "Sync failed before the first complete import.",
  degraded_stale: "Refresh failed; showing prior data.",
  needs_reconnect: "Garmin needs to be reconnected.",
};

export function GarminControls() {
  const router = useRouter();
  const [status, setStatus] = useState<GarminStatus | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [days, setDays] = useState(365);
  const [formError, setFormError] = useState<string | null>(null);
  const [isPending, setIsPending] = useState(false);
  const lastJobState = useRef<string | null>(null);

  const loadStatus = useCallback(async () => {
    const response = await fetch("/api/garmin/status", { cache: "no-store" });
    if (!response.ok) return;
    const next = (await response.json()) as GarminStatus;
    setStatus(next);
    if (
      ["requested", "running"].includes(lastJobState.current ?? "") &&
      next.job?.state === "succeeded"
    ) {
      router.refresh();
    }
    lastJobState.current = next.job?.state ?? null;
  }, [router]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const running = status?.job?.state === "requested" || status?.job?.state === "running";
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void loadStatus(), 1500);
    return () => window.clearInterval(timer);
  }, [loadStatus, running]);

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
    setIsPending(true);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!response.ok) {
        const result = (await response.json().catch(() => ({}))) as { error?: string };
        setFormError(ERROR_LABELS[result.error ?? ""] ?? "Could not start the Garmin update.");
        await loadStatus();
        return false;
      }
      lastJobState.current = "requested";
      setStatus((current) => ({
        connected: current?.connected ?? false,
        phase: endpoint.endsWith("connect") ? "credentials_stored" : "syncing",
        job: {
          state: "requested",
          kind: endpoint.endsWith("connect") ? "initial_sync" : "incremental",
        },
      }));
      return true;
    } catch {
      setFormError("Could not reach the Garmin update service.");
      return false;
    } finally {
      setIsPending(false);
    }
  };

  const connect = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isPending) return;
    const started = await startJob("/api/garmin/connect", { email, password, days });
    setPassword("");
    if (started) setModalOpen(false);
  };

  const statusLabel = status ? PHASE_LABELS[status.phase] : null;
  const canRefresh = status?.connected && status.phase !== "needs_reconnect";
  const busy = running || isPending;
  const pendingLabel = canRefresh ? "Starting Garmin refresh…" : "Connecting to Garmin…";

  return (
    <>
      <div className="flex items-center gap-2" aria-busy={busy}>
        <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {isPending ? pendingLabel : (statusLabel ?? "")}
        </span>
        {formError && !modalOpen ? (
          <span role="alert" className="text-destructive max-w-32 truncate text-xs sm:max-w-48">
            {formError}
          </span>
        ) : null}
        {statusLabel ? (
          <span
            aria-hidden="true"
            className="text-muted-foreground hidden max-w-64 truncate text-xs lg:inline"
          >
            {statusLabel}
          </span>
        ) : null}
        {canRefresh ? (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => void startJob("/api/garmin/refresh")}
          >
            <RefreshCw
              data-icon="inline-start"
              className={busy ? "animate-spin motion-reduce:animate-none" : ""}
            />
            {busy ? "Updating" : "Refresh Garmin"}
          </Button>
        ) : (
          <Button size="sm" disabled={busy} onClick={() => setModalOpen(true)}>
            {busy ? (
              <RefreshCw
                data-icon="inline-start"
                className="animate-spin motion-reduce:animate-none"
              />
            ) : (
              <Unplug data-icon="inline-start" />
            )}
            {busy
              ? "Connecting"
              : status?.phase === "needs_reconnect"
                ? "Reconnect Garmin"
                : "Connect Garmin"}
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
                  {status?.phase === "needs_reconnect" ? "Reconnect Garmin" : "Connect Garmin"}
                </h2>
                <p className="text-muted-foreground mt-1 text-sm">
                  Credentials go to the private Coach service, are encrypted before PostgreSQL
                  storage, and are never stored by the browser.
                </p>
              </div>
              <Button variant="ghost" size="icon" onClick={() => setModalOpen(false)}>
                <X />
                <span className="sr-only">Close</span>
              </Button>
            </div>
            <form className="mt-6 space-y-4" aria-busy={isPending} onSubmit={connect}>
              <div className="space-y-2">
                <Label htmlFor="garmin-email">Garmin email</Label>
                <Input
                  id="garmin-email"
                  type="email"
                  autoComplete="email"
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
              {formError ? (
                <p role="alert" className="text-destructive text-sm">
                  {formError}
                </p>
              ) : null}
              <div className="flex justify-end gap-2 pt-2">
                <Button type="button" variant="outline" onClick={() => setModalOpen(false)}>
                  Cancel
                </Button>
                <Button type="submit" disabled={isPending}>
                  {isPending ? (
                    <>
                      <RefreshCw
                        data-icon="inline-start"
                        className="animate-spin motion-reduce:animate-none"
                      />
                      Connecting…
                    </>
                  ) : (
                    "Connect and backfill"
                  )}
                </Button>
              </div>
            </form>
          </section>
        </div>
      ) : null}
    </>
  );
}
