"""Role dashboards (SESSION 16).

Framework-free: which widgets a viewer gets is a question about roles and
permissions, and answering it needs no database. Computing the numbers does,
which is why that lives in the application layer.
"""

from academic_ai.domain.dashboards.widgets import (
    ROLE_DASHBOARDS,
    WIDGETS,
    Metric,
    Severity,
    Widget,
    WidgetKind,
    widgets_for,
)

__all__ = [
    "ROLE_DASHBOARDS",
    "WIDGETS",
    "Metric",
    "Severity",
    "Widget",
    "WidgetKind",
    "widgets_for",
]
