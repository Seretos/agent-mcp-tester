"""Shared pytest fixtures and configuration."""

import pytest


@pytest.fixture
def anyio_backend():
    """Pin anyio tests to the asyncio backend (trio is not a runtime dependency)."""
    return "asyncio"
