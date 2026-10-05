# Corey Mathie, 2026
"""
Stand-ins for the server-only packages the gateway imports, so the gateway's
own modules can run unmodified in the browser (Pyodide).

Only packages that are NOT importable get a stand-in; under CPython with the
real requirements installed, install() changes nothing. The stand-ins cover
exactly the names the gateway modules import:

- fastapi: Header, HTTPException, Request, status (auth.py raises HTTPException)
- httpx: AsyncClient (raises: there is no network or model in the demo),
  HTTPError, Limits, Timeout (backends.py)
- pydantic_settings: BaseSettings, SettingsConfigDict (config.py; real pydantic
  is loaded, only the .env/environment loading layer is replaced)
"""

from __future__ import annotations

import importlib.util
import sys
import types


def _missing(name: str) -> bool:
    return name not in sys.modules and importlib.util.find_spec(name) is None


def _fastapi() -> types.ModuleType:
    mod = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail=None, headers=None):
            super().__init__(f"{status_code}: {detail}")
            self.status_code = status_code
            self.detail = detail
            self.headers = headers

    class Request:
        def __init__(self):
            self.state = types.SimpleNamespace()

    def Header(default=None, **_kwargs):
        return default

    status = types.ModuleType("fastapi.status")
    status.HTTP_401_UNAUTHORIZED = 401
    status.HTTP_403_FORBIDDEN = 403
    status.HTTP_429_TOO_MANY_REQUESTS = 429

    mod.HTTPException, mod.Request, mod.Header, mod.status = HTTPException, Request, Header, status
    sys.modules["fastapi.status"] = status
    return mod


def _httpx() -> types.ModuleType:
    mod = types.ModuleType("httpx")

    class HTTPError(Exception):
        pass

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            raise HTTPError("no network access in the browser demo; the demo backend answers locally")

    class _Config:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    mod.HTTPError, mod.AsyncClient = HTTPError, AsyncClient
    mod.Limits = type("Limits", (_Config,), {})
    mod.Timeout = type("Timeout", (_Config,), {})
    return mod


def _pydantic_settings() -> types.ModuleType:
    from pydantic import BaseModel

    mod = types.ModuleType("pydantic_settings")

    class BaseSettings(BaseModel):
        """Defaults only: the browser has no environment variables or .env file."""

    mod.BaseSettings = BaseSettings
    mod.SettingsConfigDict = dict
    return mod


def install() -> list[str]:
    """Install stand-ins for whatever is missing. Returns the names that were stubbed."""
    stubbed = []
    for name, build in (("fastapi", _fastapi), ("httpx", _httpx), ("pydantic_settings", _pydantic_settings)):
        if _missing(name):
            sys.modules[name] = build()
            stubbed.append(name)
    return stubbed
