import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-hosted in a slim container: emit a standalone server bundle.
  output: "standalone",
  // Charts/tables only — skip next/image optimization (and its sharp dependency).
  images: { unoptimized: true },
};

export default nextConfig;
