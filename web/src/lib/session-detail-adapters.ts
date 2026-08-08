import type { TrainingSession } from "@/lib/coach-client";
import { fmtDuration, fmtInt, fmtKm, fmtNum } from "@/lib/format";

export interface SessionDetailMetric {
  label: string;
  value: string;
  hint?: string;
}

export interface SessionDetailView {
  adapter: "running" | "mountaineering" | "general";
  label: string;
  metrics: SessionDetailMetric[];
}

type SessionDetailAdapter = (session: TrainingSession) => SessionDetailView;

function seconds(session: TrainingSession): number | null {
  return session.duration?.unit === "seconds" ? session.duration.value : null;
}

function metres(session: TrainingSession): number | null {
  return session.distance?.unit === "metres" ? session.distance.value : null;
}

function durationMetric(session: TrainingSession): SessionDetailMetric {
  return { label: "Duration", value: fmtDuration(seconds(session)) };
}

function distanceMetric(session: TrainingSession): SessionDetailMetric {
  return { label: "Distance", value: fmtKm(metres(session)) };
}

function rpeMetric(session: TrainingSession): SessionDetailMetric {
  return { label: "Session RPE", value: fmtInt(session.session_rpe), hint: "1–10" };
}

function pace(session: TrainingSession): string {
  const elapsed = seconds(session);
  const distance = metres(session);
  if (elapsed == null || distance == null || elapsed <= 0 || distance <= 0) return "—";
  const secondsPerKilometre = Math.round(elapsed / (distance / 1000));
  const minutes = Math.floor(secondsPerKilometre / 60);
  const remainder = String(secondsPerKilometre % 60).padStart(2, "0");
  return `${minutes}:${remainder} /km`;
}

function speed(session: TrainingSession): string {
  const elapsed = seconds(session);
  const distance = metres(session);
  if (elapsed == null || distance == null || elapsed <= 0 || distance <= 0) return "—";
  return `${fmtNum(distance / 1000 / (elapsed / 3600))} km/h`;
}

const running: SessionDetailAdapter = (session) => ({
  adapter: "running",
  label: "Running",
  metrics: [
    distanceMetric(session),
    durationMetric(session),
    { label: "Average pace", value: pace(session) },
    rpeMetric(session),
  ],
});

const mountaineering: SessionDetailAdapter = (session) => ({
  adapter: "mountaineering",
  label: "Mountaineering",
  metrics: [
    durationMetric(session),
    distanceMetric(session),
    { label: "Average speed", value: speed(session) },
    rpeMetric(session),
  ],
});

const general: SessionDetailAdapter = (session) => ({
  adapter: "general",
  label: session.sport.replaceAll("_", " "),
  metrics: [durationMetric(session), distanceMetric(session), rpeMetric(session)],
});

const adapters: Readonly<Record<string, SessionDetailAdapter>> = {
  running,
  mountaineering,
};

/** Select the sport-specific presentation without leaking that variation into pages. */
export function adaptSessionDetails(session: TrainingSession): SessionDetailView {
  return (adapters[session.sport] ?? general)(session);
}
