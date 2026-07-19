// Thin Chart.js wrapper — one config reused by every trend chart.
import Chart from "https://cdn.jsdelivr.net/npm/chart.js@4/auto/+esm";

const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

export function lineChart(canvas, { labels, data, color }) {
  const accent = color || css("--accent");
  return new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          data,
          borderColor: accent,
          backgroundColor: accent + "22",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.25,
          fill: true,
          spanGaps: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { intersect: false, mode: "index" } },
      scales: {
        x: { ticks: { color: css("--text-dim"), maxTicksLimit: 6 }, grid: { display: false } },
        y: { ticks: { color: css("--text-dim") }, grid: { color: css("--border") } },
      },
    },
  });
}
