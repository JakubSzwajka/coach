import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { mock, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
const python = process.env.GARMIN_COACH_TEST_PYTHON;
if (!python) throw new Error("GARMIN_COACH_TEST_PYTHON is required; use scripts/test-postgres");

const auth = mock.fn(async () => ({ userId: "verified-web-actor" }));
mock.module("server-only", { exports: {} });
mock.module("@clerk/nextjs/server", { exports: { auth } });
const client = await import(pathToFileURL(path.join(root, "web/src/lib/coach-client.ts")).href);

function fixture() {
  const child = spawn(python, ["-m", "tests.postgres.web_adapter_fixture"], {
    cwd: root,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  const ready = new Promise((resolve, reject) => {
    let output = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      output += chunk;
      const newline = output.indexOf("\n");
      if (newline !== -1) {
        try {
          resolve(JSON.parse(output.slice(0, newline)));
        } catch (error) {
          reject(error);
        }
      }
    });
    child.once("exit", (code) => {
      reject(new Error(`private adapter fixture exited ${code}: ${stderr}`));
    });
  });
  return { child, ready, stderr: () => stderr };
}

async function stop(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  await new Promise((resolve) => child.once("exit", resolve));
}

test("verified Clerk server auth owns tenancy across real PostgreSQL reads and mutation", async () => {
  const running = fixture();
  try {
    const ports = await running.ready;
    process.env.GARMIN_COACH_HTTP_URL = `http://127.0.0.1:${ports.port}`;
    process.env.GARMIN_COACH_HTTP_SERVICE_TOKEN = "synthetic-next-service-token";

    const dashboard = await client.getDashboard(1);
    const status = await client.getGarminStatus();
    assert.equal(dashboard.display_name, "Verified Web Actor");
    assert.equal(status.phase, "not_connected");

    const created = await client.executeAppCommand({
      type: "create_training_session",
      params: {
        content: {
          sport: "strength",
          local_date: "2026-07-22",
          title: "Signed-in server mutation",
        },
      },
    });
    assert.equal(created.revision, 1);

    await assert.rejects(
      client.executeAppCommand({
        type: "create_training_session",
        params: {
          content: { sport: "running", local_date: "2026-07-22" },
        },
        actor: "hostile-body-actor",
        profile_id: "hostile-profile-selection",
      }),
      (error) => error instanceof client.CoachClientError && error.status === 400,
    );
    const activities = await client.getActivities(90);
    assert.deepEqual(
      activities.map((session) => session.title),
      ["Signed-in server mutation"],
    );
    assert.ok(auth.mock.callCount() >= 5);

    const sentinel = fs.mkdtempSync(path.join(os.tmpdir(), "coach-legacy-sentinel-"));
    process.env.GARMIN_COACH_DATA_DIR = sentinel;
    const touched = [];
    const originals = {};
    for (const name of ["readFileSync", "writeFileSync", "openSync", "readdirSync"]) {
      originals[name] = fs[name];
    }
    const activeGuards = Object.keys(originals).map((name) =>
      mock.method(fs, name, (value, ...args) => {
        if (typeof value === "string" && value.startsWith(sentinel)) {
          touched.push(`${name}:${value}`);
          throw new Error("legacy filesystem fallback attempted");
        }
        return originals[name](value, ...args);
      }),
    );
    try {
      process.env.GARMIN_COACH_HTTP_URL = `http://127.0.0.1:${ports.outage_port}`;
      await assert.rejects(
        client.getDashboard(1),
        (error) =>
          error instanceof client.CoachClientError &&
          error.code === "application_unavailable" &&
          error.status === 503,
      );
      assert.deepEqual(touched, []);
    } finally {
      for (const guard of activeGuards) guard.mock.restore();
      fs.rmSync(sentinel, { recursive: true, force: true });
    }
  } finally {
    await stop(running.child);
  }
});
