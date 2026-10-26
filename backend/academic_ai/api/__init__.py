"""HTTP layer (SESSION 9).

Thin by design: routers translate between HTTP and the application layer and
do nothing else. Every decision worth arguing about -- whether an account is
locked, whether a principal may run an optimization, what a cancellation
means -- lives in `academic_ai.application` and `academic_ai.security`, where
it can be tested without a request object.
"""

from academic_ai.api.app import API_PREFIX, create_app

__all__ = ["API_PREFIX", "create_app"]
