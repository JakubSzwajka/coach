import { TrendsCharts } from "@/components/trends-charts";
import { Card, CardContent } from "@/components/ui/card";
import { getTrends } from "@/lib/coach-client";

export const dynamic = "force-dynamic";

export default async function TrendsPage() {
  const trends = await getTrends();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Trends</h1>
        <p className="text-muted-foreground text-sm">
          Daily wellness and training signals over time.
        </p>
      </div>
      {!trends || trends.series.length === 0 ? (
        <Card>
          <CardContent className="text-muted-foreground py-8 text-sm">
            No trend data. Connect Garmin or refresh the collector and reload.
          </CardContent>
        </Card>
      ) : (
        <TrendsCharts trends={trends} />
      )}
    </div>
  );
}
