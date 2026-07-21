import { ClerkProvider } from "@clerk/nextjs";
import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { NavBar } from "@/components/nav-bar";
import { Toaster } from "@/components/ui/sonner";
import { clerkRuntimeKeys } from "@/lib/clerk-runtime";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Garmin Coach",
  description: "Personal training dashboard over your Garmin data.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const { publishableKey } = clerkRuntimeKeys();
  return (
    <ClerkProvider publishableKey={publishableKey}>
      <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
        <body className="bg-background text-foreground flex min-h-full flex-col">
          <NavBar />
          <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8">{children}</main>
          <Toaster />
        </body>
      </html>
    </ClerkProvider>
  );
}
