"use client";

import { UserButton } from "@clerk/nextjs";
import { Activity, CalendarDays, Home, ListChecks } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { GarminControls } from "@/components/garmin-controls";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Dashboard", icon: Home },
  { href: "/calendar", label: "Calendar", icon: CalendarDays },
  { href: "/plans", label: "Plans", icon: ListChecks },
  { href: "/activities", label: "Activities", icon: Activity },
];

export function NavBar() {
  const pathname = usePathname();
  return (
    <header className="border-border border-b">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-2 px-4 sm:gap-4">
        <span className="hidden font-semibold tracking-tight sm:inline">Garmin Coach</span>
        <nav className="flex items-center gap-1">
          {links.map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-label={label}
                className={cn(
                  "text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors",
                  active && "bg-muted text-foreground",
                )}
              >
                <Icon className="size-4" />
                <span className="hidden lg:inline">{label}</span>
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <GarminControls />
          <UserButton />
        </div>
      </div>
    </header>
  );
}
