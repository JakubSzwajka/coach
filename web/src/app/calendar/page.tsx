import {
  addDays,
  addMonths,
  differenceInCalendarDays,
  endOfMonth,
  endOfWeek,
  format,
  isSameMonth,
  isToday,
  startOfMonth,
  startOfWeek,
  subMonths,
} from "date-fns";
import {
  Activity,
  CalendarCheck2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Flag,
  ListChecks,
} from "lucide-react";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { type CalendarItem, getCalendar } from "@/lib/coach-client";
import { fmtDuration, fmtKm } from "@/lib/format";
import { sessionHref } from "@/lib/session-route";
import { cn } from "@/lib/utils";

export const dynamic = "force-dynamic";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function selectedMonth(value: string | string[] | undefined): Date {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (candidate && /^\d{4}-(0[1-9]|1[0-2])$/.test(candidate)) {
    const parsed = new Date(`${candidate}-01T00:00:00`);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }
  return startOfMonth(new Date());
}

function sessionSummary(item: Extract<CalendarItem, { kind: "training_session" }>): string {
  const values = [item.sport];
  if (item.duration?.unit === "seconds") values.push(fmtDuration(item.duration.value));
  if (item.distance?.unit === "metres") values.push(fmtKm(item.distance.value));
  return values.join(" · ");
}

function plannedSummary(item: Extract<CalendarItem, { kind: "planned_session" }>): string {
  const values = [item.sport];
  if (item.target_duration_seconds != null) {
    values.push(fmtDuration(item.target_duration_seconds));
  }
  if (item.target_distance_meters != null) {
    values.push(fmtKm(item.target_distance_meters));
  }
  return values.join(" · ");
}

function CalendarEvent({ item }: { item: CalendarItem }) {
  if (item.kind === "training_session") {
    const name = item.title ?? item.session_type?.replaceAll("_", " ") ?? `${item.sport} activity`;
    return (
      <Link
        href={sessionHref(item.id)}
        className="border-emerald-600/20 bg-emerald-500/10 text-emerald-950 hover:border-emerald-600/40 dark:text-emerald-100 block rounded-md border px-2 py-1.5 transition-colors"
        title={`${name} — ${sessionSummary(item)}`}
      >
        <div className="flex min-w-0 items-center gap-1.5">
          <Activity className="size-3 shrink-0" />
          <span className="truncate text-xs font-medium capitalize">{name}</span>
        </div>
        <p className="mt-0.5 truncate text-[11px] opacity-75">{sessionSummary(item)}</p>
      </Link>
    );
  }

  if (item.kind === "planned_session") {
    const name = item.session_type?.replaceAll("_", " ") ?? `${item.sport} session`;
    const completed = item.disposition === "fulfilled";
    return (
      <div
        className={cn(
          "border-primary/20 bg-primary/8 text-foreground rounded-md border px-2 py-1.5",
          item.disposition === "cancelled" && "opacity-50 line-through",
        )}
        title={`${item.plan_name}: ${item.prescription}`}
      >
        <div className="flex min-w-0 items-center gap-1.5">
          {completed ? (
            <CalendarCheck2 className="text-primary size-3 shrink-0" />
          ) : (
            <CircleDashed className="text-primary size-3 shrink-0" />
          )}
          <span className="truncate text-xs font-medium capitalize">{name}</span>
        </div>
        <p className="text-muted-foreground mt-0.5 truncate text-[11px]">{plannedSummary(item)}</p>
      </div>
    );
  }

  return (
    <div
      className="border-amber-600/20 bg-amber-500/10 text-amber-950 dark:text-amber-100 rounded-md border px-2 py-1.5"
      title={`${item.name} — ${item.sport}`}
    >
      <div className="flex min-w-0 items-center gap-1.5">
        <Flag className="size-3 shrink-0" />
        <span className="truncate text-xs font-medium">{item.name}</span>
      </div>
      <p className="mt-0.5 truncate text-[11px] capitalize opacity-75">{item.sport} goal</p>
    </div>
  );
}

export default async function CalendarPage({
  searchParams,
}: {
  searchParams: Promise<{ month?: string | string[] }>;
}) {
  const params = await searchParams;
  const month = startOfMonth(selectedMonth(params.month));
  const gridStart = startOfWeek(month, { weekStartsOn: 1 });
  const gridEnd = endOfWeek(endOfMonth(month), { weekStartsOn: 1 });
  const days = differenceInCalendarDays(gridEnd, gridStart) + 1;
  const calendar = await getCalendar(days, format(gridEnd, "yyyy-MM-dd"));
  const items = calendar?.items ?? [];
  const byDate = new Map<string, CalendarItem[]>();
  for (const item of items) {
    const group = byDate.get(item.local_date) ?? [];
    group.push(item);
    byDate.set(item.local_date, group);
  }

  const dates = Array.from({ length: days }, (_, index) => addDays(gridStart, index));
  const inMonth = items.filter((item) => item.local_date.startsWith(format(month, "yyyy-MM")));
  const completed = inMonth.filter((item) => item.kind === "training_session").length;
  const planned = inMonth.filter((item) => item.kind === "planned_session").length;
  const goals = inMonth.filter((item) => item.kind === "goal_event").length;
  const previous = format(subMonths(month, 1), "yyyy-MM");
  const next = format(addMonths(month, 1), "yyyy-MM");

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Calendar</h1>
          <p className="text-muted-foreground text-sm">
            Completed activities, planned sessions, and goals in one view.
          </p>
        </div>
        <div className="flex items-center gap-1">
          <Link
            href={`/calendar?month=${previous}`}
            aria-label="Previous month"
            className={buttonVariants({ variant: "outline", size: "icon" })}
          >
            <ChevronLeft />
          </Link>
          <Link href="/calendar" className={buttonVariants({ variant: "outline" })}>
            Today
          </Link>
          <Link
            href={`/calendar?month=${next}`}
            aria-label="Next month"
            className={buttonVariants({ variant: "outline", size: "icon" })}
          >
            <ChevronRight />
          </Link>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Card size="sm">
          <CardContent className="flex items-center gap-3">
            <div className="bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 rounded-lg p-2">
              <Activity className="size-4" />
            </div>
            <div>
              <p className="text-xl font-semibold tabular-nums">{completed}</p>
              <p className="text-muted-foreground text-xs">Activities completed</p>
            </div>
          </CardContent>
        </Card>
        <Card size="sm">
          <CardContent className="flex items-center gap-3">
            <div className="bg-primary/10 text-primary rounded-lg p-2">
              <ListChecks className="size-4" />
            </div>
            <div>
              <p className="text-xl font-semibold tabular-nums">{planned}</p>
              <p className="text-muted-foreground text-xs">Plan entries</p>
            </div>
          </CardContent>
        </Card>
        <Card size="sm">
          <CardContent className="flex items-center gap-3">
            <div className="rounded-lg bg-amber-500/10 p-2 text-amber-700 dark:text-amber-300">
              <Flag className="size-4" />
            </div>
            <div>
              <p className="text-xl font-semibold tabular-nums">{goals}</p>
              <p className="text-muted-foreground text-xs">Goal events</p>
            </div>
          </CardContent>
        </Card>
      </div>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">{format(month, "MMMM yyyy")}</h2>
          <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-xs">
            <span className="flex items-center gap-1.5">
              <span className="size-2.5 rounded-full bg-emerald-500" /> Done
            </span>
            <span className="flex items-center gap-1.5">
              <span className="bg-primary size-2.5 rounded-full" /> Planned
            </span>
            <span className="flex items-center gap-1.5">
              <span className="size-2.5 rounded-full bg-amber-500" /> Goal
            </span>
          </div>
        </div>

        <Card className="gap-0 py-0">
          <div className="overflow-x-auto">
            <div className="min-w-[900px]">
              <div className="bg-muted/40 grid grid-cols-7 border-b">
                {WEEKDAYS.map((day) => (
                  <div key={day} className="text-muted-foreground px-3 py-2 text-xs font-medium">
                    {day}
                  </div>
                ))}
              </div>
              <div className="grid grid-cols-7">
                {dates.map((day, index) => {
                  const key = format(day, "yyyy-MM-dd");
                  const dayItems = byDate.get(key) ?? [];
                  const visible = dayItems.slice(0, 4);
                  return (
                    <div
                      key={key}
                      className={cn(
                        "min-h-40 border-r border-b p-2",
                        index % 7 === 6 && "border-r-0",
                        !isSameMonth(day, month) && "bg-muted/25 text-muted-foreground",
                      )}
                    >
                      <div className="mb-2 flex items-center justify-between">
                        <span
                          className={cn(
                            "flex size-7 items-center justify-center rounded-full text-xs font-medium",
                            isToday(day) && "bg-primary text-primary-foreground",
                          )}
                        >
                          {format(day, "d")}
                        </span>
                        {dayItems.length > 0 ? (
                          <span className="text-muted-foreground text-[10px]">
                            {dayItems.length} {dayItems.length === 1 ? "item" : "items"}
                          </span>
                        ) : null}
                      </div>
                      <div className="space-y-1.5">
                        {visible.map((item) => (
                          <CalendarEvent key={`${item.kind}:${item.id}`} item={item} />
                        ))}
                        {dayItems.length > visible.length ? (
                          <p className="text-muted-foreground px-1 text-[11px]">
                            +{dayItems.length - visible.length} more
                          </p>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </Card>
      </section>
    </div>
  );
}
