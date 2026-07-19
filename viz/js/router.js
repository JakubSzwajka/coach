import { renderDashboard } from "./views/dashboard.js";
import { renderTrends } from "./views/trends.js";
import { renderActivities } from "./views/activities.js";

const routes = {
  dashboard: renderDashboard,
  trends: renderTrends,
  activities: renderActivities,
};

export function initRouter(state) {
  const mount = document.getElementById("app");

  function render() {
    const name = (location.hash.slice(2) || "dashboard").split("/")[0];
    const view = routes[name] || renderDashboard;
    document.querySelectorAll("#nav a").forEach((a) =>
      a.classList.toggle("active", a.dataset.route === name),
    );
    try {
      view(state, mount);
    } catch (err) {
      mount.replaceChildren();
      mount.append(Object.assign(document.createElement("div"), {
        className: "loading",
        textContent: `Error rendering ${name}: ${err.message}`,
      }));
    }
  }

  window.addEventListener("hashchange", render);
  render();
}
