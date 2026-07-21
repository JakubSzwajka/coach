// Read the conventional public key dynamically so Next cannot replace it at
// build time. The server passes it explicitly to ClerkProvider and the proxy,
// allowing one Docker image to use each deployment's runtime `web/.env`.
const PUBLISHABLE_KEY_NAME = ["NEXT", "PUBLIC", "CLERK", "PUBLISHABLE", "KEY"].join("_");

export function clerkRuntimeKeys(): {
  publishableKey: string | undefined;
  secretKey: string | undefined;
} {
  return {
    publishableKey: process.env[PUBLISHABLE_KEY_NAME],
    secretKey: process.env.CLERK_SECRET_KEY,
  };
}
