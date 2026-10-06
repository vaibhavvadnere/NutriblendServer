"""
api/client.py — The one place the dashboard makes HTTP calls.

Responsibilities:
  * Base URL + timeouts.
  * Attaches the Bearer access token.
  * Refreshes the access token shortly before it expires, and once more on a 401,
    then retries the request. If refreshing fails the stored tokens are cleared
    and SessionExpired is raised, so the UI sends the user back to sign in.
  * Unwraps the server's envelope: returns `data` on success, raises a typed
    ApiError (api/errors.py) on failure.

Tokens and OTPs are never logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Protocol

import httpx

from dashboard.api.errors import (
    ApiError,
    NetworkError,
    RateLimited,
    SessionExpired,
    Unauthorized,
    error_for_status,
)
from dashboard.config import settings

logger = logging.getLogger("dashboard.api")


class TokenStore(Protocol):
    """Where the client keeps tokens. The Streamlit app uses a session-backed
    store (auth/session.py); tests use MemoryTokenStore."""

    def get_access(self) -> Optional[str]: ...
    def get_refresh(self) -> Optional[str]: ...
    def get_expires_at(self) -> Optional[float]: ...
    def set_tokens(self, access: str, refresh: str, expires_in: int) -> None: ...
    def clear(self) -> None: ...


class MemoryTokenStore:
    """A plain in-memory TokenStore."""

    def __init__(self) -> None:
        self._access: Optional[str] = None
        self._refresh: Optional[str] = None
        self._expires_at: Optional[float] = None

    def get_access(self) -> Optional[str]:
        return self._access

    def get_refresh(self) -> Optional[str]:
        return self._refresh

    def get_expires_at(self) -> Optional[float]:
        return self._expires_at

    def set_tokens(self, access: str, refresh: str, expires_in: int) -> None:
        self._access, self._refresh = access, refresh
        self._expires_at = time.time() + expires_in

    def clear(self) -> None:
        self._access = self._refresh = self._expires_at = None


class ApiClient:
    def __init__(
        self,
        tokens: TokenStore,
        http: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.tokens = tokens
        self.base_url = (base_url or settings.server_url).rstrip("/")
        self._http = http or httpx.Client(
            base_url=self.base_url,
            timeout=timeout or settings.REQUEST_TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
        files: Any = None,
        data: Optional[dict[str, Any]] = None,
        content: Any = None,  # bytes or an iterator of bytes (streamed)
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Call the API and return the envelope's `data`.

        `path` is the full server path, e.g. "/api/v1/auth/signin" (use
        settings.api_path()). `auth=False` for the public auth endpoints.
        """
        kwargs = {"json": json, "params": params, "files": files, "data": data, "content": content,
                  "extra_headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout

        if not auth:
            return self._send(method, path, None, kwargs)

        if self._access_expiring():
            self._refresh_or_expire()

        try:
            return self._send(method, path, self.tokens.get_access(), kwargs)
        except SessionExpired:
            raise
        except Unauthorized:
            # Access token rejected (expired early, server restarted with a new
            # secret, ...). Refresh once and retry; a second 401 is final.
            self._refresh_or_expire()
            try:
                return self._send(method, path, self.tokens.get_access(), kwargs)
            except Unauthorized as exc:
                self.tokens.clear()
                raise SessionExpired(exc.message, exc.code, exc.status_code, exc.details) from exc

    def refresh(self) -> None:
        """Rotate the token pair using the stored refresh token."""
        refresh_token = self.tokens.get_refresh()
        if not refresh_token:
            raise SessionExpired("Please sign in.", "UNAUTHORIZED", 401)
        payload = self._send(
            "POST", settings.api_path("/auth/refresh"), None, {"json": {"refresh_token": refresh_token}}
        )
        self.tokens.set_tokens(payload["access_token"], payload["refresh_token"], int(payload["expires_in"]))
        logger.info("Access token refreshed")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _access_expiring(self) -> bool:
        if not self.tokens.get_access():
            return bool(self.tokens.get_refresh())
        expires_at = self.tokens.get_expires_at()
        return expires_at is not None and expires_at - time.time() < settings.TOKEN_REFRESH_LEEWAY_SECONDS

    def _refresh_or_expire(self) -> None:
        try:
            self.refresh()
        except NetworkError:
            raise  # server unreachable: keep the session, the user can retry
        except ApiError as exc:
            self.tokens.clear()
            raise SessionExpired(
                "Your session has expired. Please sign in again.", exc.code, 401
            ) from exc

    def _send(self, method: str, path: str, access_token: Optional[str], kwargs: dict) -> Any:
        kwargs = dict(kwargs)
        headers = dict(kwargs.pop("extra_headers", None) or {})
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        started = time.perf_counter()
        try:
            response = self._http.request(method, path, headers=headers or None, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("%s %s timed out", method, path)
            raise NetworkError(
                f"The server at {self.base_url} took too long to respond.", "NETWORK_TIMEOUT"
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("%s %s failed: %s", method, path, type(exc).__name__)
            raise NetworkError(
                f"Cannot reach the server at {self.base_url}. Is it running?", "NETWORK_ERROR"
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.debug("%s %s -> %s (%.0f ms)", method, path, response.status_code, elapsed_ms)
        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = None

        if not isinstance(body, dict) or "success" not in body:
            raise error_for_status(response.status_code)(
                f"Unexpected response from the server (HTTP {response.status_code}).",
                "BAD_RESPONSE",
                response.status_code,
            )

        if body.get("success") and response.is_success:
            return body.get("data")

        error = body.get("error") or {}
        cls = error_for_status(response.status_code)
        args = (
            body.get("message") or "Request failed.",
            error.get("code", "INTERNAL_ERROR"),
            response.status_code,
            error.get("details"),
        )
        if cls is RateLimited:
            retry_after = response.headers.get("Retry-After")
            raise RateLimited(*args, retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None)
        raise cls(*args)
