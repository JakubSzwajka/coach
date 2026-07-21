import { readFile } from "node:fs/promises";
import path from "node:path";

// Server-only reader over the collector's file store. Mirrors the shapes the
// Python `CoachData` adapter projects into `<root>/derived`. Never import this
// from a client component — it touches the filesystem.

export interface TimelineEntry {
  date: string;
  steps?: number;
  distance_m?: number;
  active_kcal?: number;
  resting_hr?: number;
  min_hr?: number;
  max_hr?: number;
  avg_stress?: number;
  intensity_min_moderate?: number;
  intensity_min_vigorous?: number;
  floors_ascended?: number;
  sleep_seconds?: number;
  sleep_score?: number;
  deep_sleep_s?: number;
  light_sleep_s?: number;
  rem_sleep_s?: number;
  awake_s?: number;
  hrv_avg?: number;
  hrv_status?: string;
  training_readiness?: number;
  training_readiness_level?: string;
  training_status?: boolean;
  vo2max_running?: number;
  body_battery_charged?: number;
  body_battery_drained?: number;
}

export interface Athlete {
  snapshot_date?: string;
  full_name?: string;
  gender?: string;
  birth_date?: string;
  weight_g?: number | null;
  height_cm?: number | null;
  vo2max_running?: number | null;
  vo2max_cycling?: number | null;
  lactate_threshold_speed_mps?: number | null;
  lactate_threshold_hr?: number | null;
  available_training_days?: string[];
  preferred_long_training_days?: string[];
  personal_records_count?: number;
}

export interface Activity {
  activity_id: number | string;
  name?: string;
  type?: string;
  start_local?: string;
  distance_m?: number;
  duration_s?: number;
  avg_hr?: number;
  max_hr?: number;
  elevation_gain_m?: number;
  avg_speed_mps?: number;
  calories?: number;
  training_effect_aerobic?: number;
  training_effect_anaerobic?: number;
}

export interface Overview {
  athlete: Athlete | null;
  timeline: TimelineEntry[];
  activities: Activity[];
}

/**
 * Base data directory holding the store. Set `GARMIN_COACH_DATA_DIR` in
 * production (e.g. a mounted `/data`); defaults to the sibling `data/` used by
 * the collector during local development.
 */
export function dataRoot(): string {
  const configured = process.env.GARMIN_COACH_DATA_DIR?.trim();
  return configured ? path.resolve(configured) : path.resolve(process.cwd(), "..", "data");
}

async function readJson<T>(file: string, fallback: T): Promise<T> {
  try {
    return JSON.parse(await readFile(file, "utf8")) as T;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return fallback;
    throw error;
  }
}

export async function readTimeline(root: string): Promise<TimelineEntry[]> {
  let text: string;
  try {
    text = await readFile(path.join(root, "derived", "timeline.jsonl"), "utf8");
  } catch {
    return [];
  }
  const entries = text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line) as TimelineEntry);
  entries.sort((a, b) => a.date.localeCompare(b.date));
  return entries;
}

export async function readAthlete(root: string): Promise<Athlete | null> {
  const raw = await readJson<(Athlete & { user_profile_id?: number }) | null>(
    path.join(root, "derived", "athlete.json"),
    null,
  );
  if (!raw) return null;
  // Drop the Garmin profile id, matching the Python adapter's read projection.
  const { user_profile_id: _drop, ...athlete } = raw;
  return athlete;
}

export async function readActivities(root: string): Promise<Activity[]> {
  const activities = await readJson<Activity[]>(path.join(root, "derived", "activities.json"), []);
  return Array.isArray(activities) ? activities : [];
}

export async function loadOverview(root: string): Promise<Overview> {
  const [athlete, timeline, activities] = await Promise.all([
    readAthlete(root),
    readTimeline(root),
    readActivities(root),
  ]);
  return { athlete, timeline, activities };
}
