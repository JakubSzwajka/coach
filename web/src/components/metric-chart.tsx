"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fmtShortDate } from "@/lib/format";

export interface MetricPoint {
  date: string;
  value: number | null;
}

export function MetricChart({
  title,
  color,
  data,
}: {
  title: string;
  color: string;
  data: MetricPoint[];
}) {
  const hasData = data.some((d) => d.value != null);
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent className="h-[200px]">
        {hasData ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis
                dataKey="date"
                tickFormatter={fmtShortDate}
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
                minTickGap={28}
                stroke="var(--border)"
              />
              <YAxis
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
                width={40}
                stroke="var(--border)"
                domain={["auto", "auto"]}
              />
              <Tooltip
                labelFormatter={(label) => fmtShortDate(String(label))}
                contentStyle={{
                  background: "var(--popover)",
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  fontSize: 12,
                  color: "var(--popover-foreground)",
                }}
              />
              <Line type="monotone" dataKey="value" stroke={color} strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
            No data
          </div>
        )}
      </CardContent>
    </Card>
  );
}
