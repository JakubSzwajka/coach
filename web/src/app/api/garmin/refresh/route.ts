import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";
import { CoachClientError, refreshGarmin } from "@/lib/coach-client";

export const runtime = "nodejs";

export async function POST() {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  try {
    await refreshGarmin();
    return NextResponse.json({ state: "requested" }, { status: 202 });
  } catch (error) {
    if (error instanceof CoachClientError) {
      return NextResponse.json({ error: error.code }, { status: error.status });
    }
    return NextResponse.json({ error: "application_unavailable" }, { status: 503 });
  }
}
