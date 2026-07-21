import { ActivitiesTable } from "@/components/activities-table";
import { NoProfile } from "@/components/no-profile";
import { Card, CardContent } from "@/components/ui/card";
import { readActivities } from "@/lib/coach-data";
import { currentProfileRoot } from "@/lib/profile";

export const dynamic = "force-dynamic";

export default async function ActivitiesPage() {
  const root = await currentProfileRoot();
  if (!root) return <NoProfile />;
  const activities = await readActivities(root);

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
            No activities. Run the collector and reload.
          </CardContent>
        </Card>
      ) : (
        <ActivitiesTable activities={activities} />
      )}
    </div>
  );
}
