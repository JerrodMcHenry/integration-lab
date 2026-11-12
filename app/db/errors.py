"""Database error types shared by every repository.

Repositories translate SQLAlchemy/driver exceptions into these so callers
(API routes, the worker) can react without importing SQLAlchemy:

- ``RepositoryUnavailableError``: the database can't be reached (transient;
  APIs answer 503, the worker backs off).
- ``RepositoryError``: any other database failure.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout


class RepositoryError(RuntimeError):
    """Data could not be read or written."""


class RepositoryUnavailableError(RepositoryError):
    """The database could not be reached (transient)."""


def translate_db_error(exc: Exception) -> RepositoryError:
    if isinstance(exc, (OperationalError, PoolTimeout)) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    ):
        return RepositoryUnavailableError(f"database unavailable: {type(exc).__name__}")
    return RepositoryError(f"database error: {type(exc).__name__}")
