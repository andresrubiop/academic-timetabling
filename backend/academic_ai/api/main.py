"""Uvicorn entrypoint: ``uvicorn academic_ai.api.main:app``."""

from academic_ai.api.app import create_app

app = create_app()
