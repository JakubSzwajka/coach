"""Clerk-protected remote MCP server scoped to one Profile per Clerk subject."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import functools
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .profiles import ProfileRegistry
from .mcp_server import (
    activate_training_plan,
    adjust_training_plan,
    archive_training_plan,
    create_goal_event,
    create_training_plan,
    create_training_session,
    delete_goal_event,
    delete_training_plan,
    delete_training_session,
    get_goal_event,
    get_training_plan,
    get_training_plan_history,
    get_training_session,
    list_goal_events,
    list_training_plans,
    list_training_sessions,
    read_coaching_context,
    replace_goal_event,
    replace_training_session,
    reset_current_profile_root,
    set_current_profile_root,
    set_planned_session_fulfilment,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_project_env(path: Path | None = None) -> None:
    """Load project configuration without overriding explicit environment values."""
    load_dotenv(path or (_PROJECT_ROOT / ".env"), override=False)

_REMOTE_TOOLS = (
    read_coaching_context,
    create_training_session,
    list_training_sessions,
    get_training_session,
    replace_training_session,
    delete_training_session,
    create_goal_event,
    list_goal_events,
    get_goal_event,
    replace_goal_event,
    delete_goal_event,
    create_training_plan,
    get_training_plan,
    list_training_plans,
    get_training_plan_history,
    activate_training_plan,
    archive_training_plan,
    delete_training_plan,
    adjust_training_plan,
    set_planned_session_fulfilment,
)

_AUDIT_LOGGER = logging.getLogger("garmin_coach.remote_audit")
_AUDIT_LOGGER.setLevel(logging.INFO)
_AUDIT_LOGGER.propagate = False
if not _AUDIT_LOGGER.handlers:
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(logging.Formatter("%(message)s"))
    _AUDIT_LOGGER.addHandler(_audit_handler)


@dataclass(frozen=True, slots=True)
class RemoteMcpConfig:
    """Clerk OAuth resource-server configuration."""

    public_url: str
    issuer_url: str
    clerk_secret_key: str = field(repr=False)
    clerk_api_url: str = "https://api.clerk.com"

    @classmethod
    def from_env(cls) -> "RemoteMcpConfig":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        config = cls(
            public_url=required("GARMIN_COACH_MCP_PUBLIC_URL"),
            issuer_url=required("GARMIN_COACH_CLERK_ISSUER"),
            clerk_secret_key=required("GARMIN_COACH_CLERK_SECRET_KEY"),
            clerk_api_url=os.environ.get(
                "GARMIN_COACH_CLERK_API_URL",
                "https://api.clerk.com",
            ).strip(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        public = urlparse(self.public_url)
        issuer = urlparse(self.issuer_url)
        clerk_api = urlparse(self.clerk_api_url)
        if public.scheme not in {"http", "https"} or not public.netloc:
            raise ValueError("GARMIN_COACH_MCP_PUBLIC_URL must be an absolute HTTP URL")
        if (
            public.path != "/mcp"
            or public.params
            or public.query
            or public.fragment
            or public.username
            or public.password
        ):
            raise ValueError("GARMIN_COACH_MCP_PUBLIC_URL must end at the exact /mcp path")
        if issuer.scheme not in {"http", "https"} or not issuer.netloc:
            raise ValueError("GARMIN_COACH_CLERK_ISSUER must be an absolute HTTP URL")
        if (
            issuer.path
            or issuer.params
            or issuer.query
            or issuer.fragment
            or issuer.username
            or issuer.password
        ):
            raise ValueError("GARMIN_COACH_CLERK_ISSUER must be an origin without a trailing slash")
        if public.scheme != "https" and public.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("GARMIN_COACH_MCP_PUBLIC_URL must use HTTPS outside loopback")
        if issuer.scheme != "https" and issuer.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("GARMIN_COACH_CLERK_ISSUER must use HTTPS outside loopback")
        if (
            clerk_api.scheme not in {"http", "https"}
            or not clerk_api.netloc
            or clerk_api.path
            or clerk_api.params
            or clerk_api.query
            or clerk_api.fragment
            or clerk_api.username
            or clerk_api.password
        ):
            raise ValueError(
                "GARMIN_COACH_CLERK_API_URL must be an HTTP origin"
            )
        if (
            clerk_api.scheme != "https"
            and clerk_api.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError(
                "GARMIN_COACH_CLERK_API_URL must use HTTPS outside loopback"
            )


class ClerkApiTokenVerifier:
    """Verify Clerk OAuth tokens through Clerk's Backend API."""

    def __init__(
        self,
        config: RemoteMcpConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            base_url=config.clerk_api_url,
            timeout=5,
        )

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            response = await self._http_client.post(
                "/oauth_applications/access_tokens/verify",
                json={"access_token": token},
                headers={
                    "Authorization": (
                        f"Bearer {self._config.clerk_secret_key}"
                    ),
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError:
            _AUDIT_LOGGER.warning(
                "oauth_token decision=rejected reason=verification_unavailable"
            )
            return None

        if response.status_code != 200:
            _AUDIT_LOGGER.warning(
                "oauth_token decision=rejected reason=invalid_token"
            )
            return None

        try:
            token_info = response.json()
        except ValueError:
            _AUDIT_LOGGER.warning(
                "oauth_token decision=rejected reason=invalid_response"
            )
            return None

        if not isinstance(token_info, dict):
            return None
        if token_info.get("active") is False:
            return None
        if token_info.get("object") != "clerk_idp_oauth_access_token":
            return None
        if token_info.get("revoked") is not False:
            return None
        if token_info.get("expired") is not False:
            return None

        subject = token_info.get("subject")
        if not isinstance(subject, str) or not subject:
            return None

        client_id = token_info.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            return None

        scopes = token_info.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(scope, str) and scope for scope in scopes
        ):
            return None

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=None,
            resource=self._config.public_url,
            subject=subject,
            claims=token_info,
        )

class _OAuthContractMiddleware:
    def __init__(self, app: ASGIApp, config: RemoteMcpConfig) -> None:
        self._app = app
        self._config = config
        public = urlparse(config.public_url)
        self._metadata_url = (
            f"{public.scheme}://{public.netloc}"
            "/.well-known/oauth-protected-resource/mcp"
        )
        self._metadata = {
            "resource": config.public_url,
            "authorization_servers": [config.issuer_url],
            "scopes_supported": ["profile"],
            "bearer_methods_supported": ["header"],
        }

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and scope["path"]
            in {
                "/.well-known/oauth-protected-resource/mcp",
                "/.well-known/oauth-protected-resource",
            }
        ):
            await JSONResponse(self._metadata)(scope, receive, send)
            return

        async def send_with_exact_oauth_contract(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                original_headers = message.get("headers", [])
                is_scope_failure = status == 403 and any(
                    key.lower() == b"www-authenticate"
                    and b'insufficient_scope' in value
                    for key, value in original_headers
                )
                if status == 401 or is_scope_failure:
                    headers = [
                        header
                        for header in original_headers
                        if header[0].lower() != b"www-authenticate"
                    ]
                    if status == 401:
                        challenge = (
                            'Bearer error="invalid_token", '
                            'error_description="Authentication required", '
                            f'resource_metadata="{self._metadata_url}"'
                        )
                    else:
                        challenge = (
                            'Bearer error="insufficient_scope", '
                            'error_description="Required scope: profile", '
                            'scope="profile", '
                            f'resource_metadata="{self._metadata_url}"'
                        )
                    headers.append((b"www-authenticate", challenge.encode()))
                    message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_exact_oauth_contract)


class RemoteFastMCP(FastMCP):
    def __init__(
        self,
        config: RemoteMcpConfig,
        token_verifier: TokenVerifier,
    ) -> None:
        self._remote_config = config
        self._token_verifier = token_verifier
        public = urlparse(config.public_url)
        public_host = public.hostname
        assert public_host is not None
        super().__init__(
            "garmin-coach-remote",
            lifespan=self._lifespan,
            token_verifier=token_verifier,
            auth=AuthSettings(
                issuer_url=config.issuer_url,
                resource_server_url=config.public_url,
                required_scopes=["profile"],
            ),
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[
                    public.netloc,
                    public_host,
                    f"{public_host}:*",
                    "127.0.0.1:*",
                    "localhost:*",
                    "[::1]:*",
                ],
                allowed_origins=[
                    f"{public.scheme}://{public.netloc}",
                    "http://127.0.0.1:*",
                    "http://localhost:*",
                    "http://[::1]:*",
                ],
            ),
        )

    @asynccontextmanager
    async def _lifespan(self, server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            close = getattr(self._token_verifier, "aclose", None)
            if close is not None:
                await close()


    def streamable_http_app(self) -> ASGIApp:
        return _OAuthContractMiddleware(
            super().streamable_http_app(),
            self._remote_config,
        )


def _audited_tool(tool: Callable[..., Any], issuer_url: str) -> Callable[..., Any]:
    @functools.wraps(tool)
    def audited(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        access_token = get_access_token()
        subject = access_token.subject if access_token else None
        subject_hash = (
            hashlib.sha256(f"{issuer_url}\0{subject}".encode()).hexdigest()[:16]
            if subject
            else "missing"
        )
        decision = "allowed"
        base = Path(os.environ.get("GARMIN_COACH_DATA_DIR", "data"))
        profile_token = None
        if subject:
            registry = ProfileRegistry(base)
            profile = registry.bind(subject)
            root = registry.data_root(profile.id)
            profile_token = set_current_profile_root(root)
        try:
            return tool(*args, **kwargs)
        except Exception:
            decision = "error"
            raise
        finally:
            if profile_token is not None:
                reset_current_profile_root(profile_token)
            latency_ms = round((time.monotonic() - started) * 1000)
            _AUDIT_LOGGER.info(
                "remote_tool tool=%s decision=%s subject=%s latency_ms=%d",
                tool.__name__,
                decision,
                subject_hash,
                latency_ms,
            )

    return audited


def create_remote_server(
    config: RemoteMcpConfig, *, token_verifier: TokenVerifier | None = None
) -> FastMCP:
    """Build the authenticated, profile-scoped MCP registry."""
    config.validate()
    verifier = token_verifier or ClerkApiTokenVerifier(config)
    server = RemoteFastMCP(config, verifier)
    for tool in _REMOTE_TOOLS:
        server.add_tool(_audited_tool(tool, config.issuer_url))
    return server


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    """Run the Clerk-protected remote MCP server on loopback."""
    load_project_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, default=8765)
    arguments = parser.parse_args()

    config = RemoteMcpConfig.from_env()
    server = create_remote_server(config)
    server.settings.host = "127.0.0.1"
    server.settings.port = arguments.port
    server.settings.streamable_http_path = "/mcp"
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
