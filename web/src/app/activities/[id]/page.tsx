import { ArrowLeft, Database, FileText, Gauge, Timer } from "lucide-react";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CoachClientError, getTrainingSession, type TrainingSession } from "@/lib/coach-client";
import { fmtShortDate } from "@/lib/format";
import { adaptSessionDetails } from "@/lib/session-detail-adapters";
import { sessionIdFromRoute } from "@/lib/session-route";

export const dynamic = "force-dynamic";

function fullDate(localDate: string): string {
  const parsed = new Date(`${localDate}T00:00:00`);
  return Number.isNaN(parsed.getTime())
    ? localDate
    : parsed.toLocaleDateString(undefined, {
        weekday: "long",
        year: "numeric",
        month: "long",
        day: "numeric",
      });
}

function startTime(localStart: string | null): string | null {
  if (!localStart) return null;
  const parsed = new Date(localStart);
  return Number.isNaN(parsed.getTime())
    ? localStart
    : parsed.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export default async function ActivityDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const sessionId = sessionIdFromRoute(id);
  if (sessionId === null) notFound();
  let session: TrainingSession;
  try {
    session = await getTrainingSession(sessionId);
  } catch (error) {
    if (error instanceof CoachClientError && error.status === 404) notFound();
    throw error;
  }
  const details = adaptSessionDetails(session);
  const loads = session.loads ?? [];

  return (
    <div className="space-y-6">
      <Link href="/activities" className={buttonVariants({ variant: "ghost", size: "sm" })}>
        <ArrowLeft data-icon="inline-start" />
        Activities
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className="capitalize">{details.label}</Badge>
            <Badge variant="outline">
              {session.ownership === "collected" ? "Garmin" : "Manual"}
            </Badge>
            {session.revision != null ? (
              <Badge variant="secondary">Revision {session.revision}</Badge>
            ) : null}
          </div>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              {session.title ?? session.session_type?.replaceAll("_", " ") ?? details.label}
            </h1>
            <p className="text-muted-foreground text-sm">
              {fullDate(session.local_date)}
              {startTime(session.local_start) ? ` at ${startTime(session.local_start)}` : ""}
            </p>
          </div>
        </div>
      </div>

      <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {details.metrics.map((metric, index) => (
          <Card key={metric.label} size="sm">
            <CardContent className="space-y-1">
              <div className="text-muted-foreground flex items-center gap-1.5 text-xs">
                {index === 0 ? <Gauge className="size-3.5" /> : <Timer className="size-3.5" />}
                {metric.label}
              </div>
              <p className="text-xl font-semibold tabular-nums">{metric.value}</p>
              {metric.hint ? <p className="text-muted-foreground text-xs">{metric.hint}</p> : null}
            </CardContent>
          </Card>
        ))}
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Database className="size-4" />
              Session details
            </CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm">
              <dt className="text-muted-foreground">Sport</dt>
              <dd className="text-right capitalize">{session.sport.replaceAll("_", " ")}</dd>
              <dt className="text-muted-foreground">Session type</dt>
              <dd className="text-right capitalize">
                {session.session_type?.replaceAll("_", " ") ?? "—"}
              </dd>
              <dt className="text-muted-foreground">Date</dt>
              <dd className="text-right">{fmtShortDate(session.local_date)}</dd>
              <dt className="text-muted-foreground">Timing</dt>
              <dd className="text-right capitalize">
                {session.timing_precision.replaceAll("_", " ")}
              </dd>
              <dt className="text-muted-foreground">Source</dt>
              <dd className="text-right">
                {session.ownership === "collected" ? "Garmin (read-only)" : "Garmin Coach"}
              </dd>
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Training load</CardTitle>
          </CardHeader>
          <CardContent>
            {loads.length === 0 ? (
              <p className="text-muted-foreground text-sm">
                No training-load reports for this session.
              </p>
            ) : (
              <dl className="space-y-3 text-sm">
                {loads.map((load) => (
                  <div key={`${load.method}:${load.unit}`} className="flex justify-between gap-4">
                    <dt className="text-muted-foreground capitalize">
                      {load.method.replaceAll("_", " ")}
                    </dt>
                    <dd className="font-medium tabular-nums">
                      {load.value} {load.unit}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </CardContent>
        </Card>
      </div>

      {session.notes ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <FileText className="size-4" />
              Notes
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground whitespace-pre-wrap text-sm">
            {session.notes}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
