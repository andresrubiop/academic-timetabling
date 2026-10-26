"use client";

/**
 * Signing in.
 *
 * The screen that did not exist. From SESSION 9 the API client read an access
 * token out of `localStorage` and nothing ever wrote one, so the browser could
 * reach every page and authenticate to none of them: the dashboard showed its
 * error state and the timetable showed nothing, both of which looked like
 * bugs rather than "you are not signed in".
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError, signIn } from "../../lib/api/client";
import { isSignedIn, storeSession } from "../../lib/api/session";

export default function LoginPage() {
  const router = useRouter();
  // Where to go afterwards. Somebody who followed a link to a timetable and
  // was asked to sign in should land on that timetable, not on a home page
  // they then have to navigate away from.
  //
  // Read from `window.location` in an effect rather than with
  // `useSearchParams`, which forces the whole form behind a Suspense boundary
  // and leaves the server-rendered HTML empty. The form is the entire reason
  // to open this page; it should be in the first response, not in the second.
  const [next, setNext] = useState("/dashboard");

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setBusy] = useState(false);

  useEffect(() => {
    const target = new URLSearchParams(window.location.search).get("next");
    // Only a path on this site. An absolute URL here would make the sign-in
    // page an open redirect: a link to /login?next=https://elsewhere would
    // hand somebody's freshly authenticated browser to another origin.
    if (target && target.startsWith("/") && !target.startsWith("//")) {
      setNext(target);
    }
    if (isSignedIn()) router.replace(target || "/dashboard");
  }, [router]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const session = await signIn(email.trim(), password);
      storeSession(session.accessToken, session.refreshToken);
      // `replace`, not `push`: going Back to a sign-in form you have already
      // used is a dead end.
      router.replace(next);
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : "No se pudo conectar con el servidor.",
      );
      setBusy(false);
    }
  }

  return (
    <div className="page page--narrow">
      <h1 className="page__title">Iniciar sesión</h1>
      <p className="page__lede">
        Academic AI — planificación académica y horarios.
      </p>

      <form className="panel form" onSubmit={onSubmit} noValidate>
        <div className="panel__body form__body">
          <label className="field">
            Correo
            <input
              className="field__control"
              type="email"
              name="email"
              autoComplete="username"
              // Focused on load: this page exists to be typed into, and a form
              // that needs a click before it accepts a keystroke is one more
              // thing between a person and their timetable.
              autoFocus
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>

          <label className="field">
            Contraseña
            <input
              className="field__control"
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>

          {/* `role="alert"`, so a screen reader is told the attempt failed
              rather than left waiting on a button that stopped spinning. */}
          {error && (
            <p className="state state--error" role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            className="button button--primary"
            disabled={isBusy || !email || !password}
          >
            {isBusy ? "Entrando…" : "Entrar"}
          </button>
        </div>
      </form>
    </div>
  );
}

