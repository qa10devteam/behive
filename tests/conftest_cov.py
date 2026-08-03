"""Shared fixtures for deep coverage testing."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import json
import asyncio
import os
import sys


class MockPgConnection:
    """Realistic PostgreSQL connection mock that tracks SQL calls."""
    
    def __init__(self, responses=None, raise_on=None):
        self._responses = responses or {}  # {sql_fragment: [rows]}
        self._default_responses = []
        self._call_log = []
        self._raise_on = raise_on or {}
        self._cursor = self
        
    def execute(self, sql, params=None):
        self._call_log.append((sql, params))
        if self._raise_on:
            for fragment, exc in self._raise_on.items():
                if fragment in sql:
                    raise exc
        return self
    
    def executemany(self, sql, params_list):
        self._call_log.append((sql, params_list))
        return self
        
    def fetchall(self):
        # Return matching response based on last SQL
        if self._call_log:
            last_sql = self._call_log[-1][0]
            for fragment, rows in self._responses.items():
                if fragment in last_sql:
                    if isinstance(rows, list) and len(rows) > 0 and isinstance(rows[0], list):
                        return rows.pop(0)  # Consume first response
                    return rows
        return self._default_responses
    
    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None
    
    def close(self): pass
    def commit(self): pass
    def rollback(self): pass
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *a): pass


class MockLLM:
    """LLM mock that returns structured responses based on prompt content."""
    
    def __init__(self, default_response="OK"):
        self.default = default_response
        self.calls = []
        self.responses = {}  # {keyword: response}
        
    def __call__(self, prompt=None, messages=None, **kwargs):
        self.calls.append((prompt or messages, kwargs))
        text = str(prompt or messages or "")
        for kw, resp in self.responses.items():
            if kw.lower() in text.lower():
                return resp
        return self.default


@pytest.fixture
def mock_db():
    """Fixture providing MockPgConnection."""
    return MockPgConnection()


@pytest.fixture
def mock_llm():
    """Fixture providing MockLLM."""
    return MockLLM()


def run_async(coro):
    """Run async function synchronously in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
