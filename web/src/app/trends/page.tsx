import { NoProfile } from "@/components/no-profile";
import { TrendsCharts } from "@/components/trends-charts";
import { Card, CardContent } from "@/components/ui/card";
import { readTimeline } from "@/lib/coach-data";
import { currentProfileRoot } from "@/lib/profile";

export const dynamic = "force-dynamic";

export default async function TrendsPage() {
  const root = await currentProfileRoot();
  if (!root) return <NoProfile />;
  const timeline = await readTimeline(root);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Trends</h1>
        <p className="text-muted-foreground text-sm">
          Daily wellness and training signals over time.
        </p>
      </div>
      {timeline.length === 0 ? (
        <Card>
          <CardContent className="text-muted-foreground py-8 text-sm">
            No timeline data. Run the collector and reload.
          </CardContent>
        </Card>
      ) : (
        <TrendsCharts timeline={timeline} />
      )}
    </div>
  );
}
