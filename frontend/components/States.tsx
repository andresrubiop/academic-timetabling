"use client";

/**
 * Loading, empty, error and confirmation, written once.
 *
 * A "loading" that looks different on two screens reads as two different
 * states to anybody who does not know the codebase is the same one. These
 * exist so that never happens, and so each state says the one thing it is
 * actually for:
 *
 * - **Loading** holds the shape of what is coming, so the page does not jump.
 * - **Empty** says what would put something here. "No results" is a dead end;
 *   "no rooms match *aula 3* — clear the search" is a next step.
 * - **Error** says what failed and offers the retry, because an error a user
 *   cannot act on is just an apology.
 * - **Confirm** is a real dialog: focus moves into it, Escape closes it, and
 *   the destructive option is never the one focused by default.
 */

import { useEffect, useRef } from "react";

export function LoadingState({
  label = "Cargando…",
  rows = 3,
}: {
  label?: string;
  rows?: number;
}) {
  return (
    // `aria-busy` and a polite live region: a screen reader is told the wait
    // is happening, rather than being read an empty container.
    <div className="state" aria-busy="true" aria-live="polite">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: rows }).map((_, index) => (
        <div
          key={index}
          className="skeleton"
          style={{ height: "1rem", width: `${90 - index * 15}%` }}
          aria-hidden="true"
        />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="state state--centered">
      <p className="state__title">{title}</p>
      {/* The detail is the point: "no results" is a dead end, and what would
          put something here is a next step. */}
      {detail && <p>{detail}</p>}
      {action}
    </div>
  );
}

export function ErrorState({
  title = "Algo salió mal",
  detail,
  onRetry,
}: {
  title?: string;
  detail?: string;
  onRetry?: () => void;
}) {
  return (
    // `role="alert"` so it is announced when it appears. An error that only
    // sighted users learn about is not an error message.
    <div className="state state--error" role="alert">
      <p className="state__title">{title}</p>
      {detail && <p>{detail}</p>}
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Reintentar
        </button>
      )}
    </div>
  );
}

export function ConfirmDialog({
  title,
  body,
  confirmLabel = "Confirmar",
  cancelLabel = "Cancelar",
  isDestructive = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  body?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  isDestructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Where focus was before the dialog opened, so it can be given back.
    // Without this a keyboard user is returned to the top of the document
    // after every confirmation and has to find their place again.
    const opener = document.activeElement as HTMLElement | null;

    // Focus lands on *cancel*, never on the action. A dialog that opens with
    // the destructive option focused turns a stray Enter into the thing the
    // dialog existed to prevent.
    cancelRef.current?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;

      // `aria-modal` tells a screen reader the rest of the page is inert; it
      // does nothing to the tab order. Without trapping, Tab walks out of an
      // open modal into a page the user was told is not there.
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, [onCancel]);

  return (
    <div
      className="dialog-backdrop"
      // Clicking the backdrop cancels, which is the non-destructive
      // direction. Clicking inside must not.
      onClick={onCancel}
    >
      <div
        className="dialog"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="confirm-title" className="dialog__title">
          {title}
        </h2>
        {body}
        <div className="dialog__actions">
          <button
            type="button"
            className="button"
            ref={cancelRef}
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`button ${isDestructive ? "button--danger" : "button--primary"}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
