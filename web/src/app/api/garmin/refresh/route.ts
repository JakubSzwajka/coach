import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";
import { GarminJobConflict, GarminNotConnected, startGarminJob } from "@/lib/garmin-jobs";

export const runtime = "nodejs";

export async function POST() {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  try {
    await startGarminJob(userId, { action: "refresh" });
    return NextResponse.json({ state: "starting" }, { status: 202 });
  } catch (error) {
    if (error instanceof GarminJobConflict) {
      return NextResponse.json({ error: "job_running" }, { status: 409 });
    }
    if (error instanceof GarminNotConnected) {
      return NextResponse.json({ error: "not_connected" }, { status: 409 });
    }
    return NextResponse.json({ error: "job_unavailable" }, { status: 503 });
  }
}
