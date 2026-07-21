"use client";

import { UserButton } from "@clerk/nextjs";
import { Activity, Home, LineChart } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { GarminControls } from "@/components/garmin-controls";
import { cn } from "@/lib/utils";

const links = [
  { href: "/", label: "Dashboard", icon: Home },
  { href: "/trends", label: "Trends", icon: LineChart },
  { href: "/activities", label: "Activities", icon: Activity },
];

export function NavBar() {
  const pathname = usePathname();
  return (
    <header className="border-border border-b">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-6 px-4">
        <span className="font-semibold tracking-tight">Garmin Coach</span>
        <nav className="flex items-center gap-1">
          {links.map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={cn(
                  "text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors",
                  active && "bg-muted text-foreground",
                )}
              >
                <Icon className="size-4" />
                {label}
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
