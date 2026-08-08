import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";

export default function NotFound() {
  return (
    <section className="mx-auto flex max-w-lg flex-col items-center gap-4 py-16 text-center">
      <div className="space-y-2">
        <p className="text-muted-foreground text-sm font-medium">404</p>
        <h1 className="text-2xl font-semibold tracking-tight">Page not found</h1>
        <p className="text-muted-foreground text-sm">
          The page may have moved, or the requested activity is unavailable.
        </p>
      </div>
      <Link href="/" className={buttonVariants()}>
        Return to dashboard
      </Link>
    </section>
  );
}
