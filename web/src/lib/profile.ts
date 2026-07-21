import { readFile } from "node:fs/promises";
import path from "node:path";
import { auth } from "@clerk/nextjs/server";
import { dataRoot } from "./coach-data";

interface RegistryRow {
  clerk_subject?: string;
}

interface Registry {
  schema_version: number;
  profiles: Record<string, RegistryRow>;
}

async function loadRegistry(): Promise<Registry | null> {
  let registry: Registry;
  try {
    registry = JSON.parse(
      await readFile(path.join(dataRoot(), "profiles", "registry.json"), "utf8"),
    ) as Registry;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw new Error("Profile registry is unreadable", { cause: error });
  }
  if (
    registry.schema_version !== 1 ||
    !registry.profiles ||
    typeof registry.profiles !== "object" ||
    Array.isArray(registry.profiles)
  ) {
    throw new Error("Profile registry has an unsupported shape");
  }
  return registry;
}

/** Resolve only a real Clerk-subject binding; never fall back to flat data. */
export async function boundProfileRootForSubject(subject: string): Promise<string | null> {
  const registry = await loadRegistry();
  if (!registry) return null;
  const entry = Object.entries(registry.profiles).find(([, row]) => row.clerk_subject === subject);
  if (!entry) return null;
  if (!/^[0-9a-f]{32}$/.test(entry[0])) {
    throw new Error("Profile registry contains an invalid profile id");
  }
  return path.join(dataRoot(), "profiles", entry[0]);
}

/** Resolve only the profile bound to this Clerk subject; never expose flat data. */
export async function profileRootForSubject(subject: string): Promise<string | null> {
  return boundProfileRootForSubject(subject);
}

export async function currentProfileRoot(): Promise<string | null> {
  const { userId } = await auth();
  if (!userId) return null;
  return profileRootForSubject(userId);
}
