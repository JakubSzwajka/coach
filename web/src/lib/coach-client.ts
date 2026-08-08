import "server-only";

import { auth } from "@clerk/nextjs/server";

export interface ProfileProjection {
  display_name: string | null;
  revision: number;
}

export interface ObservationPoint {
  definition: string;
  status: "observed" | "missing";
  value: number | string | boolean | null;
  unit: string;
  local_date: string | null;
}

export interface TrainingSession {
  id: string;
  origin: "collected_record" | "app_record";
  ownership: "collected" | "app";
  local_date: string;
  local_start: string | null;
  timing_precision: "date_only" | "local_datetime";
  sport: string;
  session_type: string | null;
  title: string | null;
  duration: { value: number; unit: string; basis: string } | null;
  distance: { value: number; unit: string } | null;
  session_rpe: number | null;
  loads: Array<{ value: number; unit: string; method: string; source: string }> | null;
  notes: string | null;
  revision: number | null;
  created_at: string;
  updated_at: string;
}

export interface CollectionHealth {
  records: number;
  captures: number;
  latest_collected_at: string | null;
  latest_training_session_date: string | null;
  latest_observation_date: string | null;
  sources: Array<{
    provider: string;
    state: string;
    domains: Array<{
      domain: string;
      outcome: string;
      safe_code: string | null;
      attempted_at: string | null;
      finished_at: string | null;
      last_success_at: string | null;
    }>;
  }>;
}

export interface DashboardProjection {
  starts_on: string;
  ends_on: string;
  display_name: string | null;
  latest_observations: ObservationPoint[];
  recent_sessions: TrainingSession[];
  collection_health: CollectionHealth;
}

export interface TrendSeries {
  definition: string;
  value_type: string;
  unit: string;
  window_kind: string;
  method: string;
  points: Array<{
    status: string;
    value: number | string | boolean | null;
    local_date: string | null;
    observed_at: string | null;
    window_start: string | null;
    window_end: string | null;
  }>;
}

export interface TrendsProjection {
  starts_on: string;
  ends_on: string;
  series: TrendSeries[];
}

export interface PlannedSession {
  id: string;
  scheduled_date: string;
  sport: string;
  session_type: string | null;
  prescription: string;
  target_duration_seconds: number | null;
  target_distance_meters: number | null;
  effort_guidance: string | null;
  disposition: "scheduled" | "fulfilled" | "skipped" | "cancelled";
  fulfilment_note: string | null;
  matches: string[];
}

export interface TrainingPlan {
  id: string;
  origin: "app_record";
  name: string;
  starts_on: string;
  ends_on: string;
  status: "draft" | "active" | "archived";
  requires_review: boolean;
  goal_events: Array<Record<string, unknown>>;
  constraints: string[];
  planned_sessions: PlannedSession[];
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface CalendarTrainingSession {
  kind: "training_session";
  id: string;
  local_date: string;
  local_start: string | null;
  timing_precision: "date_only" | "local_datetime";
  origin: "collected_record" | "app_record";
  ownership: "collected" | "app";
  sport: string;
  session_type: string | null;
  title: string | null;
  duration: { value: number; unit: string; basis: string } | null;
  distance: { value: number; unit: string } | null;
  notes: string | null;
  annotation: {
    id: string;
    revision: number;
    notes: string | null;
    reliability: string | null;
    duplicate: boolean;
  } | null;
  plan_matches: Array<{
    plan_id: string;
    plan_name: string;
    plan_status: string;
    plan_revision: number;
    planned_session_id: string;
  }>;
  revision: number | null;
}

export interface CalendarPlannedSession {
  kind: "planned_session";
  id: string;
  local_date: string;
  plan_id: string;
  plan_name: string;
  plan_status: "draft" | "active" | "archived";
  plan_revision: number;
  plan_requires_review: boolean;
  sport: string;
  session_type: string | null;
  prescription: string;
  target_duration_seconds: number | null;
  target_distance_meters: number | null;
  effort_guidance: string | null;
  disposition: "scheduled" | "fulfilled" | "skipped" | "cancelled";
  fulfilment_note: string | null;
  matches: Array<{
    training_session_id: string;
    ownership: "collected" | "app";
    local_date: string;
    sport: string;
  }>;
}

export interface CalendarGoalEvent {
  kind: "goal_event";
  id: string;
  local_date: string;
  local_start: string | null;
  timing_precision: "date_only" | "local_datetime";
  sport: string;
  name: string;
  priority: string;
  status: string;
  distance: { value: number; unit: string } | null;
  notes: string | null;
  revision: number;
  requires_review: boolean;
}

export type CalendarItem = CalendarTrainingSession | CalendarPlannedSession | CalendarGoalEvent;

export interface CalendarProjection {
  starts_on: string;
  ends_on: string;
  items: CalendarItem[];
}

export type GarminConnectionPhase =
  | "not_connected"
  | "credentials_stored"
  | "authenticating"
  | "syncing"
  | "first_sync_complete"
  | "connected"
  | "degraded"
  | "degraded_stale"
  | "needs_reconnect";

export interface GarminStatus {
  connected: boolean;
  phase: GarminConnectionPhase;
  safe_code?: string | null;
  last_success_at?: string | null;
  job: {
    state: "requested" | "running" | "succeeded" | "failed";
    kind: "initial_sync" | "incremental";
    safe_code: string | null;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
  } | null;
}

export type AppCommand =
  | {
      type: "update_profile_display_name";
      params: { expected_revision: number; display_name: string | null };
    }
  | { type: "create_training_session"; params: { content: Record<string, unknown> } }
  | {
      type: "replace_training_session";
      params: { id: string; expected_revision: number; content: Record<string, unknown> };
    }
  | { type: "delete_training_session"; params: { id: string; expected_revision: number } }
  | { type: "create_goal_event"; params: { content: Record<string, unknown> } }
  | {
      type: "replace_goal_event";
      params: { id: string; expected_revision: number; content: Record<string, unknown> };
    }
  | { type: "delete_goal_event"; params: { id: string; expected_revision: number } }
  | {
      type: "create_training_plan";
      params: {
        name: string;
        starts_on: string;
        ends_on: string;
        reason: string;
        goal_events?: Array<Record<string, unknown>>;
        constraints?: string[];
        planned_sessions?: Array<Record<string, unknown>>;
      };
    }
  | {
      type: "activate_training_plan" | "archive_training_plan";
      params: { id: string; expected_revision: number; reason: string };
    }
  | { type: "delete_training_plan"; params: { id: string; expected_revision: number } }
  | {
      type: "adjust_training_plan";
      params: {
        id: string;
        expected_revision: number;
        reason: string;
        effective_from: string;
        operations: Array<Record<string, unknown>>;
        name?: string;
        starts_on?: string;
        ends_on?: string;
        goal_events?: Array<Record<string, unknown>>;
        constraints?: string[];
      };
    }
  | {
      type: "set_planned_session_fulfilment";
      params: {
        id: string;
        expected_revision: number;
        planned_session_id: string;
        disposition: string;
        reason: string;
        matches?: string[];
        fulfilment_note?: string;
      };
    };

export class CoachClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;

  constructor(code: string, status: number, retryable: boolean) {
    super(code);
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

function config(): { baseUrl: string; serviceToken: string } {
  const baseUrl = process.env.GARMIN_COACH_HTTP_URL?.trim();
  const serviceToken = process.env.GARMIN_COACH_HTTP_SERVICE_TOKEN?.trim();
  if (!baseUrl || !/^https?:\/\//.test(baseUrl) || !serviceToken) {
    throw new CoachClientError("client_configuration_error", 503, false);
  }
  return { baseUrl: baseUrl.replace(/\/$/, ""), serviceToken };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { userId } = await auth();
  if (!userId) throw new CoachClientError("unauthorized", 401, false);
  const { baseUrl, serviceToken } = config();
  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      ...init,
      cache: "no-store",
      headers: {
        ...init?.headers,
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        Authorization: `Bearer ${serviceToken}`,
        "X-Garmin-Coach-Clerk-Subject": userId,
      },
    });
  } catch {
    throw new CoachClientError("application_unavailable", 503, true);
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      error?: { code?: string; retryable?: boolean };
    } | null;
    throw new CoachClientError(
      payload?.error?.code ?? "application_error",
      response.status,
      payload?.error?.retryable ?? response.status >= 500,
    );
  }
  return (await response.json()) as T;
}

export function getProfile(): Promise<ProfileProjection | null> {
  return request("/v1/profile");
}

export function getDashboard(days = 30): Promise<DashboardProjection | null> {
  return request(`/v1/dashboard?days=${days}`);
}

export async function getActivities(days = 90): Promise<TrainingSession[]> {
  const result = await request<{ sessions: TrainingSession[] }>(`/v1/activities?days=${days}`);
  return result.sessions;
}

export function getTrainingSession(id: string): Promise<TrainingSession> {
  return request(`/v1/app/training-sessions/${id}`);
}

export function getTrends(days = 90): Promise<TrendsProjection | null> {
  return request(`/v1/trends?days=${days}`);
}

export async function getTrainingPlans(): Promise<TrainingPlan[]> {
  const result = await request<{ plans: TrainingPlan[] }>("/v1/app/training-plans");
  return result.plans;
}

export function getCalendar(days: number, endDate: string): Promise<CalendarProjection | null> {
  return request(`/v1/calendar?days=${days}&end_date=${encodeURIComponent(endDate)}`);
}

export function getGarminStatus(): Promise<GarminStatus> {
  return request("/v1/garmin/status");
}

export function connectGarmin(email: string, password: string, days: number): Promise<unknown> {
  return request("/v1/garmin/connect", {
    method: "POST",
    body: JSON.stringify({ email, password, days }),
  });
}

export function refreshGarmin(): Promise<unknown> {
  return request("/v1/garmin/refresh", { method: "POST" });
}

export function executeAppCommand<T>(command: AppCommand): Promise<T> {
  return request("/v1/app/execute", { method: "POST", body: JSON.stringify(command) });
}
