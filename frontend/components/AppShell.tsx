"use client";

/**
 * Navigation, and the frame every page sits in.
 *
 * Before this there was none: SESSION 14 exposed twenty entities over HTTP and
 * SESSION 16 built seven dashboards, and the only way to reach the timetable
 * was to type its URL. Navigation to nowhere is not navigation.
 *
 * The skip link is first in the tab order on purpose. Without one, a keyboard
 * user tabs through every navigation item on every page before reaching the
 * thing they came for -- which on the timetable is a grid with hundreds of
 * focusable cells.
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { fetchMe, signOut } from "../lib/api/client";
import { clearSession, isSignedIn, refreshToken } from "../lib/api/session";

const LINKS = [
  { href: "/dashboard", label: "Panel" },
  { href: "/timetable", label: "Horario" },
  { href: "/requests", label: "Solicitudes" },
  { href: "/users", label: "Personas" },
];

/** Pages a signed-out visitor may see. Everything else redirects. */
const PUBLIC = ["/login", "/"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [who, setWho] = useState<string | null>(null);

  // Send a signed-out visitor to the sign-in screen rather than to a page
  // that renders its error state. "No se pudo cargar el panel" for somebody
  // who simply has not signed in reads as a broken platform.
  useEffect(() => {
    if (PUBLIC.includes(pathname)) return;
    if (!isSignedIn()) {
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
      return;
    }
    // The role comes from the server on every load, not from the token: a
    // token minted before a demotion would otherwise show the old one.
    fetchMe()
      .then((me) => setWho(me.role))
      .catch(() => {
        // An expired or revoked token looks exactly like never having had
        // one, and the answer is the same.
        clearSession();
        router.replace(`/login?next=${encodeURIComponent(pathname)}`);
      });
  }, [pathname, router]);

  async function onSignOut() {
    const refresh = refreshToken();
    clearSession();
    setWho(null);
    // Revoke server-side after clearing locally, so a failure to reach the
    // API still signs this browser out.
    await signOut(refresh);
    router.replace("/login");
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Saltar al contenido
      </a>
      <header className="shell__nav">
        <span className="shell__brand">Academic&nbsp;AI</span>
        <nav aria-label="Principal">
          <ul className="shell__links">
            {LINKS.map((link) => {
              const isCurrent =
                pathname === link.href || pathname.startsWith(`${link.href}/`);
              return (
                <li key={link.href}>
                  <Link
                    href={link.href}
                    className="shell__link"
                    // The styling follows aria-current rather than a separate
                    // class, so what a screen reader is told and what a sighted
                    // user sees cannot drift apart.
                    aria-current={isCurrent ? "page" : undefined}
                  >
                    {link.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <span className="shell__spacer" />
        {who && (
          <>
            <span className="shell__who">{who}</span>
            <button type="button" className="button" onClick={onSignOut}>
              Salir
            </button>
          </>
        )}
      </header>
      <main id="main" className="shell__main" tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}
