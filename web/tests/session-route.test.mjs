import assert from "node:assert/strict";
import path from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

const root = path.resolve(import.meta.dirname, "../..");
const routes = await import(pathToFileURL(path.join(root, "web/src/lib/session-route.ts")).href);
const id = `session:${"1".repeat(32)}`;

test("session links keep the path-safe colon and legacy encoded routes resolve", () => {
  assert.equal(routes.sessionHref(id), `/activities/${id}`);
  assert.equal(routes.sessionIdFromRoute(encodeURIComponent(id)), id);
  assert.equal(routes.sessionIdFromRoute(id), id);
});

test("invalid route identifiers stay unavailable", () => {
  assert.equal(routes.sessionIdFromRoute("not-a-session"), null);
  assert.throws(() => routes.sessionHref("not-a-session"), /invalid session id/);
});
