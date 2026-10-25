"""ASGI entry point: `uvicorn heybuddy_backend.main:app` (settings from the environment)."""
from .app import create_app

app = create_app()
