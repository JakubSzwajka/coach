"use client";

import { useState } from "react";
import { MetricChart } from "@/components/metric-chart";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { TimelineEntry } from "@/lib/coach-data";

type Range = "30" | "90" | "all";

const SERIES: { title: string; color: string; value: (r: TimelineEntry) => number | null }[] = [
  {
    title: "Training readiness",
    color: "var(--chart-1)",
    value: (r) => r.training_readiness ?? null,
  },
  { title: "HRV (avg)", color: "var(--chart-2)", value: (r) => r.hrv_avg ?? null },
  { title: "Resting HR", color: "var(--chart-3)", value: (r) => r.resting_hr ?? null },
  {
    title: "Sleep (hours)",
    color: "var(--chart-4)",
    value: (r) => (r.sleep_seconds != null ? r.sleep_seconds / 3600 : null),
  },
  { title: "Avg stress", color: "var(--chart-5)", value: (r) => r.avg_stress ?? null },
  { title: "VO₂max (running)", color: "var(--chart-1)", value: (r) => r.vo2max_running ?? null },
];

export function TrendsCharts({ timeline }: { timeline: TimelineEntry[] }) {
  const [range, setRange] = useState<Range>("30");
  const rows = range === "all" ? timeline : timeline.slice(-Number(range));

  return (
    <div className="space-y-4">
      <Tabs value={range} onValueChange={(v) => setRange(v as Range)}>
        <TabsList>
          <TabsTrigger value="30">30d</TabsTrigger>
          <TabsTrigger value="90">90d</TabsTrigger>
          <TabsTrigger value="all">All</TabsTrigger>
        </TabsList>
      </Tabs>
      <div className="grid gap-4 sm:grid-cols-2">
        {SERIES.map((s) => (
          <MetricChart
            key={s.title}
            title={s.title}
            color={s.color}
            data={rows.map((r) => ({ date: r.date, value: s.value(r) }))}
          />
        ))}
      </div>
    </div>
  );
}
