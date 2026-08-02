"""Configuration, read from environment variables only.

No secrets in code or tests. This module reads `DATABASE_URL` and nothing else
database-shaped — in particular it never reads `TEST_DATABASE_URL`, so a test
run cannot reach production by importing the wrong thing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

CURRENCY = "PHP"
TIMEZONE = "Asia/Manila"


class ConfigError(RuntimeError):
    """A required environment variable is missing or unusable."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Production settings. Never call this from a test."""
    return Settings(database_url=_require("DATABASE_URL"))
