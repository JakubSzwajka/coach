"""Fail-closed PostgreSQL configuration without file-store fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


class DatabaseConfigurationError(RuntimeError):
    """PostgreSQL configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Validated runtime database settings.

    The original URL is intentionally excluded from ``repr`` so credentials do
    not leak through diagnostics.
    """

    _url: str

    @classmethod
    def from_env(
        cls, variable: str = "GARMIN_COACH_DATABASE_URL"
    ) -> "DatabaseSettings":
        url = os.environ.get(variable, "").strip()
        if not url:
            raise DatabaseConfigurationError(f"{variable} is required")
        return cls.from_url(url)

    @classmethod
    def from_url(cls, url: str) -> "DatabaseSettings":
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise DatabaseConfigurationError(
                "GARMIN_COACH_DATABASE_URL must be a valid PostgreSQL URL"
            ) from exc
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or not parsed.username
            or not parsed.path.lstrip("/")
            or parsed.fragment
        ):
            raise DatabaseConfigurationError(
                "GARMIN_COACH_DATABASE_URL must be a PostgreSQL URL with "
                "user, host, and database"
            )
        return cls(url)

    @property
    def url(self) -> str:
        """Return the validated URL for trusted database adapters only."""
        return self._url

    @property
    def sqlalchemy_url(self) -> str:
        """Select Psycopg 3 explicitly for SQLAlchemy/Alembic."""
        _, remainder = self._url.split("://", 1)
        return f"postgresql+psycopg://{remainder}"

    def __repr__(self) -> str:
        return "DatabaseSettings(url=<redacted>)"
