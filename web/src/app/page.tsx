import { NoProfile } from "@/components/no-profile";
import { StatCard } from "@/components/stat-card";
import { TrendsCharts } from "@/components/trends-charts";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getDashboard, getTrends } from "@/lib/coach-client";
import { fmtHoursMin, fmtInt, fmtNum, fmtShortDate } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const [dashboard, trends] = await Promise.all([getDashboard(), getTrends()]);
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
        <div>
          <h2 className="text-sm font-semibold tracking-tight">Trends</h2>
          <p className="text-muted-foreground text-sm">
            Daily wellness and training signals over time.
          </p>
        </div>
        {!trends || trends.series.length === 0 ? (
          <p className="text-muted-foreground text-sm">No trend data collected.</p>
        ) : (
          <TrendsCharts trends={trends} />
        )}
      </section>
    </div>
  );
}
