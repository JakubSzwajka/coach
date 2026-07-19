import { el, km, duration, pace, round } from "../util.js";

const COLS = [
  { key: "start_local", label: "Date", num: false, get: (a) => (a.start_local || "").slice(0, 10) },
  { key: "type", label: "Type", num: false, get: (a) => a.type || "–" },
  { key: "name", label: "Name", num: false, get: (a) => a.name || "–" },
  { key: "distance_m", label: "Dist (km)", num: true, get: (a) => km(a.distance_m) },
  { key: "duration_s", label: "Time", num: true, get: (a) => duration(a.duration_s) },
  { key: "pace", label: "Pace", num: true, get: (a) => pace(a.distance_m, a.duration_s) },
  { key: "avg_hr", label: "Avg HR", num: true, get: (a) => round(a.avg_hr) ?? "–" },
  { key: "elevation_gain_m", label: "Elev (m)", num: true, get: (a) => round(a.elevation_gain_m) ?? "–" },
  { key: "training_effect_aerobic", label: "TE aero", num: true, get: (a) => round(a.training_effect_aerobic, 1) ?? "–" },
];

export function renderActivities({ activities }, mount) {
  let sortKey = "start_local";
  let dir = -1;

  const tbody = el("tbody");
  const render = () => {
    const rows = [...activities].sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      if (x == null) return 1;
      if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * dir;
    });
    tbody.replaceChildren(
      ...rows.map((a) =>
        el("tr", {},
          ...COLS.map((c) => el("td", { class: c.num ? "num" : "" },
            c.key === "type" ? el("span", { class: "pill" }, c.get(a)) : c.get(a))),
        ),
      ),
    );
  };

  const head = el("tr", {},
    ...COLS.map((c) =>
      el("th", {
        class: c.num ? "num" : "",
        onclick: () => {
          dir = sortKey === c.key ? -dir : (c.num ? -1 : 1);
          sortKey = c.key;
          render();
        },
      }, c.label),
    ),
  );

  render();
  mount.replaceChildren(
    el("div", { class: "section-title" }, `Activities (${activities.length})`),
    el("table", {}, el("thead", {}, head), tbody),
  );
}
