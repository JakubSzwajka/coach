"use client";

import { useState } from "react";
import { MetricChart } from "@/components/metric-chart";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { TrendsProjection } from "@/lib/coach-client";

type Range = "30" | "90";

const SERIES = [
  { definition: "daily_training_readiness", title: "Training readiness", color: "var(--chart-1)" },
  { definition: "nightly_hrv_average", title: "HRV (avg)", color: "var(--chart-2)" },
  { definition: "daily_resting_heart_rate", title: "Resting HR", color: "var(--chart-3)" },
  {
    definition: "daily_sleep_duration",
    title: "Sleep (hours)",
    color: "var(--chart-4)",
    divisor: 3600,
  },
  { definition: "daily_average_stress", title: "Avg stress", color: "var(--chart-5)" },
  { definition: "daily_vo2max_running", title: "VO₂max (running)", color: "var(--chart-1)" },
] as const;

export function TrendsCharts({ trends }: { trends: TrendsProjection }) {
  const [range, setRange] = useState<Range>("30");

  return (
    <div className="space-y-4">
      <Tabs value={range} onValueChange={(value) => setRange(value as Range)}>
        <TabsList>
          <TabsTrigger value="30">30d</TabsTrigger>
          <TabsTrigger value="90">90d</TabsTrigger>
        </TabsList>
      </Tabs>
      <div className="grid gap-4 sm:grid-cols-2">
        {SERIES.map((definition) => {
          const series = trends.series.find((item) => item.definition === definition.definition);
          const points = series?.points.slice(-Number(range));
          return (
            <MetricChart
              key={definition.definition}
              title={definition.title}
              color={definition.color}
              data={(points ?? []).map((point) => ({
                date: point.local_date ?? point.observed_at ?? "",
                value:
                  typeof point.value === "number"
                    ? point.value / ("divisor" in definition ? definition.divisor : 1)
                    : null,
              }))}
            />
          );
        })}
      </div>
    </div>
  );
}
