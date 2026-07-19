// The ONE seam that knows where data lives and how it's shaped.
// Views never fetch or touch file paths — they call these functions.
// A future React port / different backend swaps only this file.

const BASE = "/data/derived";

async function json(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

async function jsonl(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  const text = await res.text();
  return text
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
}

export async function loadAll() {
  const [timeline, athlete, activities] = await Promise.all([
    jsonl(`${BASE}/timeline.jsonl`),
    json(`${BASE}/athlete.json`).catch(() => ({})),
    json(`${BASE}/activities.json`).catch(() => []),
  ]);
  timeline.sort((a, b) => a.date.localeCompare(b.date));
  return { timeline, athlete, activities };
}
