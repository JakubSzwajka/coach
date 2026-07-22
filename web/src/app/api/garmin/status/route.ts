import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";
import { CoachClientError, getGarminStatus } from "@/lib/coach-client";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  try {
    return NextResponse.json(await getGarminStatus(), {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (error) {
    if (error instanceof CoachClientError) {
      return NextResponse.json({ error: error.code }, { status: error.status });
    }
    return NextResponse.json({ error: "application_unavailable" }, { status: 503 });
  }
}
