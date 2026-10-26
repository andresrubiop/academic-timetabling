/**
 * Where the access token lives, and the only place that decides so.
 *
 * The token was read from `localStorage` by the API client from SESSION 9
 * onward and **nothing ever wrote it**: there was no sign-in screen, so the
 * browser could reach the platform and never authenticate. This module is the
 * writing half.
 *
 * `localStorage` rather than a cookie, because the API takes a bearer token in
 * the `Authorization` header and nothing here uses cookies — which is also why
 * `SECURITY.md` records that CSRF protection is not needed yet. Introducing a
 * cookie flow would make CSRF a live requirement, so this deliberately does
 * not.
 *
 * The trade is stated rather than hidden: a token in `localStorage` is
 * readable by any script running on this origin, so an XSS bug becomes a
 * session theft. The mitigations that matter are elsewhere — no `innerHTML`
 * with server data, the CSP the proxy sets, and short access-token lifetimes
 * (30 minutes, from `.env`).
 */

const ACCESS = "access_token";
const REFRESH = "refresh_token";

/** Every read is guarded: a private window or blocked site data throws. */
function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* A browser refusing to store is not a reason to fail the sign-in; the
       session simply does not survive a reload. */
  }
}

export function accessToken(): string | null {
  if (typeof window === "undefined") return null;
  return read(ACCESS);
}

export function refreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return read(REFRESH);
}

export function isSignedIn(): boolean {
  return accessToken() !== null;
}

export function storeSession(access: string, refresh: string | null): void {
  if (typeof window === "undefined") return;
  write(ACCESS, access);
  write(REFRESH, refresh);
}

export function clearSession(): void {
  if (typeof window === "undefined") return;
  write(ACCESS, null);
  write(REFRESH, null);
}
