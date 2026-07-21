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
import { loadOverview } from "@/lib/coach-data";
import { fmtHoursMin, fmtInt, fmtKm, fmtNum, fmtShortDate } from "@/lib/format";
import { currentProfileRoot } from "@/lib/profile";

// Data is read from files at request time so the dashboard reflects the latest
// collector run without a rebuild.
export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const root = await currentProfileRoot();
  if (!root) return <NoProfile />;
  const { athlete, timeline, activities } = await loadOverview(root);
  const latest = timeline.at(-1);
  const recent = activities.slice(0, 5);

  if (!latest && !athlete) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>No data yet</CardTitle>
        </CardHeader>
        <CardContent className="text-muted-foreground text-sm">
          Nothing under <code className="text-foreground">derived/</code>. Run the collector (
          <code className="text-foreground">python -m collector.collect</code>) and reload.
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {athlete?.full_name ?? "Athlete"}
          </h1>
          <p className="text-muted-foreground text-sm">
            Latest data {fmtShortDate(latest?.date)}
            {athlete?.snapshot_date
              ? ` · profile as of ${fmtShortDate(athlete.snapshot_date)}`
              : ""}
          </p>
        </div>
        {latest?.training_readiness_level ? (
          <Badge variant="secondary" className="text-xs">
            Readiness {latest.training_readiness_level}
          </Badge>
        ) : null}
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard
          label="Training readiness"
          value={fmtInt(latest?.training_readiness)}
          hint={latest?.training_readiness_level ?? undefined}
        />
        <StatCard label="Resting HR" value={fmtInt(latest?.resting_hr)} hint="bpm" />
        <StatCard
          label="HRV"
          value={fmtInt(latest?.hrv_avg)}
          hint={latest?.hrv_status && latest.hrv_status !== "NONE" ? latest.hrv_status : "ms"}
        />
        <StatCard
          label="Sleep"
          value={fmtHoursMin(latest?.sleep_seconds)}
          hint={latest?.sleep_score != null ? `score ${latest.sleep_score}` : undefined}
        />
        <StatCard
          label="Body battery"
          value={fmtInt(latest?.body_battery_charged)}
          hint="charged"
        />
        <StatCard label="Avg stress" value={fmtInt(latest?.avg_stress)} />
        <StatCard label="VO₂max (run)" value={fmtNum(latest?.vo2max_running)} />
        <StatCard label="Steps" value={fmtInt(latest?.steps)} />
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
                  <TableRow key={String(a.activity_id)}>
                    <TableCell className="whitespace-nowrap">
                      {fmtShortDate(a.start_local)}
                    </TableCell>
                    <TableCell className="max-w-[280px] truncate font-medium">{a.name}</TableCell>
                    <TableCell>
                      {a.type ? (
                        <Badge variant="outline" className="capitalize">
                          {a.type}
                        </Badge>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmtKm(a.distance_m)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtInt(a.avg_hr)}</TableCell>
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
