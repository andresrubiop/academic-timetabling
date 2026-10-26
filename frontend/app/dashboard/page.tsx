"use client";

/**
 * The dashboard.
 *
 * SESSION 16 built twenty-four widgets and seven role dashboards, and until
 * now nothing rendered any of them: the server assembled a screen nobody
 * could open. This is that screen, and it is presentational only -- which
 * role sees which widget is decided by `widgets_for()` against the caller's
 * own permissions, and there is deliberately no way to ask for somebody
 * else's.
 */

import { useCallback, useEffect, useState } from "react";

import { MetricCard } from "../../components/MetricCard";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { fetchDashboard, type Dashboard } from "../../lib/api/client";
import { isWide, orderMetrics } from "../../lib/dashboard/present";

export default function DashboardPage() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    let cancelled = false;
    fetchDashboard()
      .then((result) => {
        if (!cancelled) setDashboard(result);
      })
      .catch((cause: Error) => {
        if (!cancelled) setError(cause.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => load(), [load]);

  return (
    <div className="page">
      <h1 className="page__title">Panel</h1>
      {dashboard && (
        <p className="page__lede">
          Lo que corresponde a tu rol: <strong>{dashboard.role}</strong>. Cada
          panel se muestra según tus permisos.
        </p>
      )}

      {isLoading && <LoadingState label="Cargando el panel…" rows={4} />}

      {/* Retry rather than an apology: the usual cause is a token that has
          expired or a backend that was restarting. */}
      {!isLoading && error && (
        <ErrorState
          title="No se pudo cargar el panel"
          detail={error}
          onRetry={load}
        />
      )}

      {!isLoading && !error && dashboard && dashboard.metrics.length === 0 && (
        <EmptyState
          title="Todavía no hay nada que mostrar"
          detail="Tu rol no tiene paneles asignados, o el periodo académico aún no tiene datos cargados."
        />
      )}

      {!isLoading && !error && dashboard && dashboard.metrics.length > 0 && (
        <div className="metrics">
          {orderMetrics(dashboard.metrics).map((metric) => (
            <div
              key={metric.widgetCode}
              className={isWide(metric) ? "metrics__item--wide" : undefined}
            >
              <MetricCard metric={metric} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
