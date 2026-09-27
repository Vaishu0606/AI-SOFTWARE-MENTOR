"""Structured logging configuration."""

import logging
import sys

from app.core.config import settings


def configure_logging() -> None:
    """Configure application-wide logging."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    # Suppress noisy third-party loggers in development
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
