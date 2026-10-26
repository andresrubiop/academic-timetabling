"use client";

/**
 * People, and what each of them is responsible for.
 *
 * The screen that makes SESSIONS 23-26 usable. Row-level authorisation was
 * built, tested and deployed, and there was no way for an administrator to
 * assign anybody a scope — or to create a second account at all — without a
 * shell on the container. A permission model nobody can administer is one
 * nobody uses.
 */

import { useCallback, useEffect, useState } from "react";

import { ConfirmDialog, EmptyState, ErrorState, LoadingState } from "../../components/States";
import {
  createUser,
  fetchProgramOptions,
  fetchScopes,
  fetchUsers,
  grantScope,
  revokeScope,
  setUserActive,
  type FilterOption,
  type ScopeGrant,
  type UserAccount,
} from "../../lib/api/client";

const ROLES = [
  "SUPER_ADMIN",
  "ADMIN",
  "DEAN",
  "DIRECTOR",
  "COORDINATOR",
  "TEACHER",
  "STUDENT",
];

/** Roles whose authority comes from an assignment rather than from the role. */
const SCOPED = new Set(["DEAN", "DIRECTOR", "COORDINATOR"]);

export default function UsersPage() {
  const [users, setUsers] = useState<UserAccount[] | null>(null);
  const [programs, setPrograms] = useState<FilterOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<UserAccount | null>(null);
  const [deactivating, setDeactivating] = useState<UserAccount | null>(null);

  const load = useCallback(() => {
    setError(null);
    Promise.all([fetchUsers(), fetchProgramOptions()])
      .then(([found, options]) => {
        setUsers(found);
        setPrograms(options);
      })
      .catch((cause: Error) => setError(cause.message));
  }, []);

  useEffect(() => load(), [load]);

  async function onCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setError(null);
    try {
      const account = await createUser(
        String(form.get("email") ?? ""),
        String(form.get("full_name") ?? ""),
        String(form.get("role") ?? "COORDINATOR"),
      );
      // Shown once, deliberately. It is not stored anywhere it could be read
      // back, so a page reload loses it — which is why it is not tucked into
      // a row somebody has to notice.
      setCreated(account);
      event.currentTarget.reset();
      load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo crear.");
    }
  }

  if (error && users === null) {
    return (
      <div className="page">
        <h1 className="page__title">Personas</h1>
        <ErrorState title="No se pudo cargar" detail={error} onRetry={load} />
      </div>
    );
  }

  return (
    <div className="page">
      <h1 className="page__title">Personas</h1>
      <p className="page__lede">
        Quién entra, con qué rol, y de qué carrera responde. Un rol de
        coordinación sin carrera asignada no ve nada — es lo correcto, y es lo
        que hay que asignar aquí.
      </p>

      {error && (
        <p className="state state--error" role="alert">
          {error}
        </p>
      )}

      {created && (
        <section className="panel">
          <div className="panel__header">
            <h2 className="panel__title">Cuenta creada: {created.email}</h2>
          </div>
          <div className="panel__body">
            <p>
              Contraseña temporal — <strong>se muestra una sola vez</strong> y
              no se puede volver a consultar. Quien la reciba deberá cambiarla
              antes de poder usar la plataforma.
            </p>
            <p className="metric__value" style={{ userSelect: "all" }}>
              {created.temporaryPassword}
            </p>
            <button
              type="button"
              className="button"
              onClick={() => setCreated(null)}
            >
              Ya la copié
            </button>
          </div>
        </section>
      )}

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">Añadir una persona</h2>
        </div>
        <div className="panel__body">
          <form className="toolbar" onSubmit={onCreate}>
            <label className="field">
              Correo
              <input
                className="field__control"
                type="email"
                name="email"
                required
              />
            </label>
            <label className="field">
              Nombre
              <input className="field__control" type="text" name="full_name" />
            </label>
            <label className="field">
              Rol
              <select className="field__control" name="role" defaultValue="COORDINATOR">
                {ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className="button button--primary">
              Crear
            </button>
          </form>
        </div>
      </section>

      {users === null ? (
        <LoadingState label="Cargando las personas…" rows={4} />
      ) : users.length === 0 ? (
        <EmptyState title="No hay cuentas todavía" />
      ) : (
        <ul className="plain-list">
          {users.map((user) => (
            <UserRow
              key={user.userId}
              user={user}
              programs={programs}
              onDeactivate={() => setDeactivating(user)}
              onChanged={load}
            />
          ))}
        </ul>
      )}

      {deactivating && (
        <ConfirmDialog
          title={`¿Desactivar a ${deactivating.email}?`}
          confirmLabel="Desactivar"
          isDestructive
          body={
            <p className="metric__detail">
              No se borra: una cuenta que publicó un horario o aprobó un cambio
              no puede eliminarse sin borrar quién lo hizo. Deja de poder
              entrar, y se puede reactivar.
            </p>
          }
          onCancel={() => setDeactivating(null)}
          onConfirm={async () => {
            const target = deactivating;
            setDeactivating(null);
            try {
              await setUserActive(target.userId, false);
              load();
            } catch (cause) {
              setError(cause instanceof Error ? cause.message : "Falló.");
            }
          }}
        />
      )}
    </div>
  );
}

function UserRow({
  user,
  programs,
  onDeactivate,
  onChanged,
}: {
  user: UserAccount;
  programs: FilterOption[];
  onDeactivate: () => void;
  onChanged: () => void;
}) {
  const [scopes, setScopes] = useState<ScopeGrant[] | null>(null);
  const [isOpen, setOpen] = useState(false);
  const needsScope = SCOPED.has(user.role);

  useEffect(() => {
    if (!isOpen || scopes !== null) return;
    fetchScopes(user.userId)
      .then(setScopes)
      .catch(() => setScopes([]));
  }, [isOpen, scopes, user.userId]);

  const labelOf = (id: string) =>
    programs.find((p) => p.id === id)?.label ?? id;

  return (
    <li className="panel">
      <div className="panel__header">
        <h3 className="panel__title">
          {user.fullName || user.email}{" "}
          <span className="badge badge--accent">{user.role}</span>{" "}
          {!user.isActive && <span className="badge">inactiva</span>}
          {user.mustChangePassword && (
            <span className="badge badge--warning">contraseña temporal</span>
          )}
        </h3>
      </div>
      <div className="panel__body">
        <p className="metric__detail">{user.email}</p>
        <div className="side-panel__actions">
          {needsScope && (
            <button
              type="button"
              className="button"
              onClick={() => setOpen((open) => !open)}
            >
              {isOpen ? "Ocultar carreras" : "Carreras asignadas"}
            </button>
          )}
          {user.isActive && (
            <button type="button" className="button button--danger" onClick={onDeactivate}>
              Desactivar
            </button>
          )}
          {!user.isActive && (
            <button
              type="button"
              className="button"
              onClick={async () => {
                await setUserActive(user.userId, true);
                onChanged();
              }}
            >
              Reactivar
            </button>
          )}
        </div>

        {isOpen && (
          <div className="side-panel__section">
            {scopes === null ? (
              <LoadingState label="Cargando ámbitos…" rows={1} />
            ) : scopes.length === 0 ? (
              // The deny-by-default rule, said plainly where somebody can act
              // on it rather than left as an empty list.
              <p className="state">
                Sin carreras asignadas, así que no ve ninguna. Asigna una
                abajo.
              </p>
            ) : (
              <ul className="plain-list">
                {scopes.map((scope) => (
                  <li key={scope.assignmentId}>
                    {scope.programId ? labelOf(scope.programId) : "facultad"}{" "}
                    <button
                      type="button"
                      className="button"
                      onClick={async () => {
                        await revokeScope(user.userId, scope.assignmentId);
                        setScopes(null);
                        setOpen(true);
                      }}
                    >
                      Quitar
                    </button>
                  </li>
                ))}
              </ul>
            )}

            <form
              className="toolbar"
              onSubmit={async (event) => {
                event.preventDefault();
                const form = new FormData(event.currentTarget);
                const programId = String(form.get("program") ?? "");
                if (!programId) return;
                await grantScope(user.userId, { programId });
                setScopes(null);
              }}
            >
              <label className="field">
                Asignar carrera
                <select className="field__control" name="program" defaultValue="">
                  <option value="">elige…</option>
                  {programs.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit" className="button">
                Asignar
              </button>
            </form>
          </div>
        )}
      </div>
    </li>
  );
}
