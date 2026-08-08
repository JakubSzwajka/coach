import { AlertTriangle, CalendarDays, CheckCircle2, CircleDashed, ListChecks } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { getTrainingPlans, type PlannedSession, type TrainingPlan } from "@/lib/coach-client";
import { fmtDuration, fmtKm } from "@/lib/format";

export const dynamic = "force-dynamic";

function calendarDate(iso: string, options?: Intl.DateTimeFormatOptions): string {
  const parsed = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, options ?? { month: "short", day: "numeric" });
}

function statusVariant(status: TrainingPlan["status"]): "default" | "secondary" | "outline" {
  if (status === "active") return "default";
  if (status === "draft") return "secondary";
  return "outline";
}

function disposition(session: PlannedSession) {
  if (session.disposition === "fulfilled") {
    return (
      <Badge variant="default">
        <CheckCircle2 data-icon="inline-start" />
        Fulfilled
      </Badge>
    );
  }
  if (session.disposition === "skipped" || session.disposition === "cancelled") {
    return <Badge variant="outline">{session.disposition}</Badge>;
  }
  return (
    <Badge variant="secondary">
      <CircleDashed data-icon="inline-start" />
      Scheduled
    </Badge>
  );
}

function target(session: PlannedSession): string {
  const values: string[] = [];
  if (session.target_duration_seconds != null) {
    values.push(fmtDuration(session.target_duration_seconds));
  }
  if (session.target_distance_meters != null) {
    values.push(fmtKm(session.target_distance_meters));
  }
  return values.length > 0 ? values.join(" · ") : "Flexible";
}

function PlanCard({ plan }: { plan: TrainingPlan }) {
  const fulfilled = plan.planned_sessions.filter(
    (session) => session.disposition === "fulfilled",
  ).length;

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>{plan.name}</span>
          <Badge variant={statusVariant(plan.status)} className="capitalize">
            {plan.status}
          </Badge>
          {plan.requires_review ? (
            <Badge variant="destructive">
              <AlertTriangle data-icon="inline-start" />
              Review needed
            </Badge>
          ) : null}
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span>
            {calendarDate(plan.starts_on)}–
            {calendarDate(plan.ends_on, { month: "short", day: "numeric", year: "numeric" })}
          </span>
          <span>
            {fulfilled}/{plan.planned_sessions.length} sessions fulfilled
          </span>
          <span>Revision {plan.revision}</span>
        </CardDescription>
        <CardAction>
          <CalendarDays className="text-muted-foreground size-5" />
        </CardAction>
      </CardHeader>

      {plan.constraints.length > 0 ? (
        <CardContent className="space-y-2">
          <h2 className="text-xs font-semibold tracking-wide uppercase">Plan notes</h2>
          <ul className="text-muted-foreground grid gap-1 text-sm">
            {plan.constraints.map((constraint) => (
              <li key={constraint} className="flex gap-2">
                <span aria-hidden="true">•</span>
                <span>{constraint}</span>
              </li>
            ))}
          </ul>
        </CardContent>
      ) : null}

      <div className="overflow-x-auto border-t">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Session</TableHead>
              <TableHead>Prescription</TableHead>
              <TableHead>Target</TableHead>
              <TableHead>Effort</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {plan.planned_sessions.map((session) => (
              <TableRow key={session.id}>
                <TableCell className="whitespace-nowrap font-medium">
                  {calendarDate(session.scheduled_date, {
                    weekday: "short",
                    month: "short",
                    day: "numeric",
                  })}
                </TableCell>
                <TableCell className="capitalize">
                  {session.session_type?.replaceAll("_", " ") ?? session.sport}
                </TableCell>
                <TableCell className="min-w-64 max-w-md">{session.prescription}</TableCell>
                <TableCell className="whitespace-nowrap tabular-nums">{target(session)}</TableCell>
                <TableCell className="text-muted-foreground min-w-48">
                  {session.effort_guidance ?? "—"}
                </TableCell>
                <TableCell>{disposition(session)}</TableCell>
              </TableRow>
            ))}
            {plan.planned_sessions.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="text-muted-foreground py-8 text-center">
                  This plan has no scheduled sessions.
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </div>
    </Card>
  );
}

export default async function TrainingPlansPage() {
  const plans = await getTrainingPlans();
  const active = plans.filter((plan) => plan.status === "active").length;
  const sessions = plans.reduce((count, plan) => count + plan.planned_sessions.length, 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Training plans</h1>
          <p className="text-muted-foreground text-sm">
            Scheduled coaching work stored in your private PostgreSQL profile.
          </p>
        </div>
        {plans.length > 0 ? (
          <div className="text-muted-foreground flex gap-4 text-sm">
            <span>{plans.length} plans</span>
            <span>{active} active</span>
            <span>{sessions} sessions</span>
          </div>
        ) : null}
      </div>

      {plans.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <ListChecks className="text-muted-foreground size-8" />
            <div>
              <p className="font-medium">No training plans yet</p>
              <p className="text-muted-foreground mt-1 text-sm">
                Plans created through Garmin Coach or its local MCP will appear here.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-6">
          {plans.map((plan) => (
            <PlanCard key={plan.id} plan={plan} />
          ))}
        </div>
      )}
    </div>
  );
}
