const SESSION_ID = /^(?:session|app):[0-9a-f]{32}$/;

export function sessionHref(id: string): string {
  if (!SESSION_ID.test(id)) throw new Error("invalid session id");
  return `/activities/${id}`;
}

export function sessionIdFromRoute(value: string): string | null {
  let decoded: string;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    return null;
  }
  return SESSION_ID.test(decoded) ? decoded : null;
}
