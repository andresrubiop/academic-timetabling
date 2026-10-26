"""Framework-free domain layer.

Nothing in this package may import SQLAlchemy, FastAPI, Alembic, psycopg, or
any other framework. `tests/test_domain_purity.py` enforces this.
"""
