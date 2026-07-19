import { el, hours, round } from "../util.js";

function card(label, value, unit, sub) {
  return el("div", { class: "card" },
    el("div", { class: "label" }, label),
    el("div", { class: "value" }, value ?? "–", unit ? el("span", { class: "unit" }, unit) : null),
    sub ? el("div", { class: "sub" }, sub) : null,
  );
}

function age(birth) {
  if (!birth) return null;
  const d = new Date(birth);
  return Math.floor((Date.now() - d) / (365.25 * 864e5));
}

export function renderDashboard({ timeline, athlete }, mount) {
  const latest = timeline[timeline.length - 1] || {};
  const a = athlete || {};

  const facts = el("div", { class: "facts" },
    a.vo2max_running ? el("span", {}, "VO₂max ", el("b", {}, a.vo2max_running)) : null,
    a.weight_g ? el("span", {}, "Weight ", el("b", {}, round(a.weight_g / 1000, 1) + " kg")) : null,
    a.height_cm ? el("span", {}, "Height ", el("b", {}, round(a.height_cm) + " cm")) : null,
    age(a.birth_date) ? el("span", {}, "Age ", el("b", {}, age(a.birth_date))) : null,
    a.lactate_threshold_hr ? el("span", {}, "LT HR ", el("b", {}, a.lactate_threshold_hr)) : null,
    a.preferred_long_training_days?.length
      ? el("span", {}, "Long days ", el("b", {}, a.preferred_long_training_days.join(", ").toLowerCase()))
      : null,
  );

  const header = el("div", { class: "athlete" },
    el("h1", {}, a.full_name || "Athlete"),
    facts,
  );

  const cards = el("div", { class: "cards" },
    card("Readiness", latest.training_readiness, "", latest.training_readiness_level),
    card("Sleep", hours(latest.sleep_seconds), "h", latest.sleep_score ? `score ${latest.sleep_score}` : null),
    card("HRV", latest.hrv_avg, "ms", latest.hrv_status && latest.hrv_status !== "NONE" ? latest.hrv_status : null),
    card("Resting HR", latest.resting_hr, "bpm"),
    card("Body Battery", latest.body_battery_charged != null ? `+${latest.body_battery_charged}` : null, "", latest.body_battery_drained != null ? `−${latest.body_battery_drained} drained` : null),
    card("Steps", latest.steps?.toLocaleString?.() ?? latest.steps),
    card("Avg Stress", latest.avg_stress),
    card("VO₂max", latest.vo2max_running, "", "running"),
  );

  mount.replaceChildren(
    header,
    el("div", { class: "section-title" }, `Latest — ${latest.date || ""}`),
    cards,
  );
}
