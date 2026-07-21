import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";
import { GarminJobConflict, startGarminJob } from "@/lib/garmin-jobs";

export const runtime = "nodejs";

interface ConnectBody {
  email?: unknown;
  password?: unknown;
  days?: unknown;
}

export async function POST(request: Request) {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  let body: ConnectBody;
  try {
    body = (await request.json()) as ConnectBody;
  } catch {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }
  const email = typeof body.email === "string" ? body.email.trim() : "";
  const password = typeof body.password === "string" ? body.password : "";
  const days = body.days ?? 365;
  if (
    !email ||
    email.length > 320 ||
    !password ||
    password.length > 512 ||
    !Number.isInteger(days) ||
    Number(days) < 1 ||
    Number(days) > 730
  ) {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }

  try {
    await startGarminJob(userId, { action: "connect", email, password, days: Number(days) });
    return NextResponse.json({ state: "starting" }, { status: 202 });
  } catch (error) {
    if (error instanceof GarminJobConflict) {
      return NextResponse.json({ error: "job_running" }, { status: 409 });
    }
    return NextResponse.json({ error: "job_unavailable" }, { status: 503 });
  }
}
