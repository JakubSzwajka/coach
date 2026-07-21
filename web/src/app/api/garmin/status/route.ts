import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";
import { getGarminStatus } from "@/lib/garmin-jobs";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const { userId } = await auth();
  if (!userId) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  try {
    return NextResponse.json(await getGarminStatus(userId), {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ error: "status_unavailable" }, { status: 503 });
  }
}
