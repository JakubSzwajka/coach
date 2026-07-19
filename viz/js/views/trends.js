import { el, hours, shortDate } from "../util.js";
import { lineChart } from "../charts/line.js";

const SERIES = [
  { key: "training_readiness", title: "Training readiness", color: "#4ea1ff" },
  { key: "hrv_avg", title: "HRV (ms)", color: "#4ad991" },
  { key: "sleep_hours", title: "Sleep (hours)", color: "#b28dff" },
  { key: "resting_hr", title: "Resting HR (bpm)", color: "#ff6b6b" },
  { key: "avg_stress", title: "Avg stress", color: "#ffce55" },
  { key: "vo2max_running", title: "VO₂max (running)", color: "#4ec7ff" },
];

let charts = [];

function draw({ timeline }, mount, days) {
  charts.forEach((c) => c.destroy());
  charts = [];

  const rows = days ? timeline.slice(-days) : timeline;
  const labels = rows.map((r) => shortDate(r.date));

  const grid = el("div", { class: "charts" });
  for (const s of SERIES) {
    const data = rows.map((r) =>
      s.key === "sleep_hours" ? hours(r.sleep_seconds) : r[s.key] ?? null,
    );
    const canvas = el("canvas");
    grid.append(el("div", { class: "chart-box" }, el("h3", {}, s.title), canvas));
    // defer so the canvas is in the DOM before Chart measures it
    queueMicrotask(() => charts.push(lineChart(canvas, { labels, data, color: s.color })));
  }
  const old = mount.querySelector(".charts");
  if (old) old.replaceWith(grid);
  else mount.append(grid);
}

export function renderTrends(state, mount) {
  const ranges = [
    { label: "30d", days: 30 },
    { label: "90d", days: 90 },
    { label: "All", days: 0 },
  ];
  let active = 90;

  const toggle = el("div", { class: "range" });
  const buttons = ranges.map((r) => {
    const b = el("button", {
      class: r.days === active ? "active" : "",
      onclick: () => {
        active = r.days;
        buttons.forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        draw(state, mount, active);
      },
    }, r.label);
    return b;
  });
  toggle.append(...buttons);

  mount.replaceChildren(el("div", { class: "section-title" }, "Trends"), toggle);
  draw(state, mount, active);
}
