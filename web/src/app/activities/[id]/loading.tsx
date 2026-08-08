import { Skeleton } from "@/components/ui/skeleton";

export default function ActivityDetailLoading() {
  return (
    <div className="space-y-6" role="status" aria-label="Loading activity details">
      <span className="sr-only">Loading activity details…</span>
      <Skeleton className="h-7 w-24" aria-hidden="true" />
      <div className="space-y-3" aria-hidden="true">
        <Skeleton className="h-6 w-28" />
        <Skeleton className="h-8 w-full max-w-md" />
        <Skeleton className="h-4 w-52" />
      </div>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4" aria-hidden="true">
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
      </div>
      <div className="grid gap-6 lg:grid-cols-2" aria-hidden="true">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    </div>
  );
}
