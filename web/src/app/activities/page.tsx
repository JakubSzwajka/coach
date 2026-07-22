import { ActivitiesTable } from "@/components/activities-table";
import { Card, CardContent } from "@/components/ui/card";
import { getActivities } from "@/lib/coach-client";

export const dynamic = "force-dynamic";

export default async function ActivitiesPage() {
  const activities = await getActivities();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Activities</h1>
        <p className="text-muted-foreground text-sm">
          {activities.length} collected training {activities.length === 1 ? "session" : "sessions"}.
        </p>
      </div>
      {activities.length === 0 ? (
        <Card>
          <CardContent className="text-muted-foreground py-8 text-sm">
            No activities. Connect Garmin or refresh the collector and reload.
          </CardContent>
        </Card>
      ) : (
        <ActivitiesTable activities={activities} />
      )}
    </div>
  );
}
