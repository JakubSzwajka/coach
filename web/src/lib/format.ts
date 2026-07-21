// Missing is missing — never fabricate a value to fill a gap.
const DASH = "—";

export function fmtInt(n?: number | null): string {
  return n == null ? DASH : Math.round(n).toLocaleString();
}

export function fmtNum(n?: number | null, digits = 1): string {
  return n == null ? DASH : n.toFixed(digits);
}

export function fmtKm(meters?: number | null): string {
  return meters == null ? DASH : `${(meters / 1000).toFixed(2)} km`;
}

/** Whole-session clock: h:mm:ss when hours are present, else m:ss. */
export function fmtDuration(seconds?: number | null): string {
  if (seconds == null) return DASH;
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (v: number) => String(v).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Sleep/rest duration as `Nh Mm`. */
export function fmtHoursMin(seconds?: number | null): string {
  if (seconds == null) return DASH;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

export function fmtShortDate(iso?: string): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
