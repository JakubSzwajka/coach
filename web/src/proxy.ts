import { clerkMiddleware } from "@clerk/nextjs/server";
import { clerkRuntimeKeys } from "@/lib/clerk-runtime";

// Mandatory auth: every matched route requires a signed-in Clerk user.
// Unauthenticated requests are redirected to Clerk's hosted sign-in.
const keys = clerkRuntimeKeys();
export default clerkMiddleware(async (auth) => {
  await auth.protect();
}, keys);

export const config = {
  matcher: [
    // Run on everything except Next internals and files with an extension.
    "/((?!_next/static|_next/image|favicon.ico|.*\\.[\\w]+$).*)",
  ],
};
