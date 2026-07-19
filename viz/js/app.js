import { loadAll } from "./data.js";
import { initRouter } from "./router.js";

const mount = document.getElementById("app");

try {
  const state = await loadAll();
  initRouter(state);
} catch (err) {
  mount.replaceChildren();
  mount.append(Object.assign(document.createElement("div"), {
    className: "loading",
    textContent:
      `Could not load data (${err.message}). ` +
      `Serve from the repo root: python -m http.server 8000, then open /viz/.`,
  }));
}
