import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function NoProfile() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>No profile linked</CardTitle>
      </CardHeader>
      <CardContent className="text-muted-foreground text-sm">
        Your account isn&apos;t bound to an athlete profile yet. Use <strong>Connect Garmin</strong>{" "}
        in the navigation bar to create your isolated profile. Existing legacy data is never claimed
        automatically.
      </CardContent>
    </Card>
  );
}
