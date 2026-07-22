import { NoProfile } from "@/components/no-profile";
import { StatCard } from "@/components/stat-card";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { getDashboard } from "@/lib/coach-client";
import { fmtHoursMin, fmtInt, fmtKm, fmtNum, fmtShortDate } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const dashboard = await getDashboard();
  if (!dashboard) return <NoProfile />;
  const latest = Object.fromEntries(
    dashboard.latest_observations.map((item) => [item.definition, item]),
  );
  const value = (definition: string) => latest[definition]?.value;
  const numeric = (definition: string) => {
    const candidate = value(definition);
    return typeof candidate === "number" ? candidate : undefined;
  };
  const text = (definition: string) => {
    const candidate = value(definition);
    return typeof candidate === "string" ? candidate : undefined;
  };
  const recent = dashboard.recent_sessions;

  if (dashboard.collection_health.records === 0 && !dashboard.display_name) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>No data yet</CardTitle>
        </CardHeader>
        <CardContent className="text-muted-foreground text-sm">
          Connect Garmin to start the first PostgreSQL-backed sync, then reload.
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {dashboard.display_name ?? "Athlete"}
          </h1>
          <p className="text-muted-foreground text-sm">
            Latest data{" "}
            {fmtShortDate(dashboard.collection_health.latest_observation_date ?? undefined)}
          </p>
        </div>
        {text("daily_training_readiness_level") ? (
          <Badge variant="secondary" className="text-xs">
            Readiness {text("daily_training_readiness_level")}
          </Badge>
        ) : null}
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard
          label="Training readiness"
          value={fmtInt(numeric("daily_training_readiness"))}
          hint={text("daily_training_readiness_level")}
        />
        <StatCard
          label="Resting HR"
          value={fmtInt(numeric("daily_resting_heart_rate"))}
          hint="bpm"
        />
        <StatCard
          label="HRV"
          value={fmtInt(numeric("nightly_hrv_average"))}
          hint={text("daily_hrv_status") ?? "ms"}
        />
        <StatCard
          label="Sleep"
          value={fmtHoursMin(numeric("daily_sleep_duration"))}
          hint={
            numeric("daily_sleep_score") != null
              ? `score ${numeric("daily_sleep_score")}`
              : undefined
          }
        />
        <StatCard
          label="Body battery"
          value={fmtInt(numeric("daily_body_battery_charged"))}
          hint="charged"
        />
        <StatCard label="Avg stress" value={fmtInt(numeric("daily_average_stress"))} />
        <StatCard label="VO₂max (run)" value={fmtNum(numeric("daily_vo2max_running"))} />
        <StatCard label="Steps" value={fmtInt(numeric("daily_steps"))} />
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold tracking-tight">Recent activities</h2>
        {recent.length === 0 ? (
          <p className="text-muted-foreground text-sm">No activities collected.</p>
        ) : (
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Date</TableHead>
                  <TableHead>Activity</TableHead>
                  <TableHead>Sport</TableHead>
                  <TableHead className="text-right">Distance</TableHead>
                  <TableHead className="text-right">Avg HR</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {recent.map((a) => (
                  <TableRow key={a.id}>
                    <TableCell className="whitespace-nowrap">
                      {fmtShortDate(a.local_start ?? a.local_date)}
                    </TableCell>
                    <TableCell className="max-w-[280px] truncate font-medium">{a.title}</TableCell>
                    <TableCell>
                      {a.sport ? (
                        <Badge variant="outline" className="capitalize">
                          {a.sport}
                        </Badge>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {fmtKm(a.distance?.unit === "metres" ? a.distance.value : undefined)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">—</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}
      </section>
    </div>
  );
}
