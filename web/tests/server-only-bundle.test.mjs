import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

function files(root) {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(root, entry.name);
    return entry.isDirectory() ? files(target) : [target];
  });
}

test("private adapter service credentials are absent from browser bundles", () => {
  const browserRoot = path.join(process.cwd(), ".next", "static");
  assert.ok(fs.existsSync(browserRoot), "run pnpm build before the bundle test");
  const browser = files(browserRoot)
    .map((file) => fs.readFileSync(file))
    .join("\n");
  assert.ok(!browser.includes("server-only-bundle-sentinel"));
  assert.ok(!browser.includes("GARMIN_COACH_HTTP_SERVICE_TOKEN"));
});
