from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp.server.auth.provider import AccessToken

from coach.remote_mcp_server import (
    ClerkApiTokenVerifier,
    RemoteMcpConfig,
    create_remote_server,
    load_project_env,
)


class _RejectingTokenVerifier:
    async def verify_token(self, token: str):
        return None


class _InsufficientScopeTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken:
        return AccessToken(
            token=token,
            client_id="oauth_client",
            scopes=["email"],
            expires_at=int(time.time()) + 300,
            resource="https://mcp.example.test/mcp",
            subject="user_owner",
        )





def _config(
    *,
    public_url: str = "https://mcp.example.test/mcp",
    clerk_api_url: str = "https://api.clerk.example.test",
) -> RemoteMcpConfig:
    return RemoteMcpConfig(
        public_url=public_url,
        issuer_url="https://clerk.example.test",
        clerk_secret_key="sk_test_secret",
        clerk_api_url=clerk_api_url,
    )


class RemoteMcpHttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_authless_request_discovers_clerk_through_protected_resource_metadata(
        self,
    ) -> None:
        config = _config()
        server = create_remote_server(config, token_verifier=_RejectingTokenVerifier())
        transport = httpx.ASGITransport(app=server.streamable_http_app())

        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as client:
            unauthorized = await client.post("/mcp", json={})
            metadata = await client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(
            unauthorized.headers["www-authenticate"],
            'Bearer error="invalid_token", '
            'error_description="Authentication required", '
            'resource_metadata="https://mcp.example.test/'
            '.well-known/oauth-protected-resource/mcp"',
        )
        self.assertEqual(
            metadata.json(),
            {
                "resource": "https://mcp.example.test/mcp",
                "authorization_servers": ["https://clerk.example.test"],
                "scopes_supported": ["profile"],
                "bearer_methods_supported": ["header"],
            },
        )

    async def test_token_without_profile_scope_is_forbidden(self) -> None:
        config = _config()
        server = create_remote_server(
            config,
            token_verifier=_InsufficientScopeTokenVerifier(),
        )
        transport = httpx.ASGITransport(app=server.streamable_http_app())

        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://mcp.example.test",
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            response = await client.post("/mcp", json={})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.headers["www-authenticate"],
            'Bearer error="insufficient_scope", '
            'error_description="Required scope: profile", '
            'scope="profile", '
            'resource_metadata="https://mcp.example.test/'
            '.well-known/oauth-protected-resource/mcp"',
        )


    async def test_verifier_accepts_an_allowed_user_from_any_verified_client(
        self,
    ) -> None:
        config = _config()

        def verify(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.url,
                httpx.URL(
                    "https://api.clerk.example.test/"
                    "oauth_applications/access_tokens/verify"
                ),
            )
            self.assertEqual(
                request.headers["Authorization"],
                "Bearer sk_test_secret",
            )
            self.assertEqual(
                json.loads(request.content),
                {"access_token": "oauth-access-token"},
            )
            return httpx.Response(
                200,
                json={
                    "object": "clerk_idp_oauth_access_token",
                    "id": "oat_test",
                    "client_id": "dynamically_registered_client",
                    "subject": "user_friend",
                    "scopes": ["profile", "offline_access"],
                    "revoked": False,
                    "revocation_reason": None,
                    "expired": False,
                    "expiration": 1_800_000_000,
                    "created_at": 1_700_000_000,
                    "updated_at": 1_700_000_000,
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(verify),
            base_url=config.clerk_api_url,
        ) as client:
            verifier = ClerkApiTokenVerifier(config, http_client=client)
            access_token = await verifier.verify_token("oauth-access-token")

        self.assertIsNotNone(access_token)
        assert access_token is not None
        self.assertEqual(
            access_token.client_id,
            "dynamically_registered_client",
        )
        self.assertEqual(access_token.subject, "user_friend")
        self.assertEqual(access_token.resource, "https://mcp.example.test/mcp")
        self.assertEqual(access_token.scopes, ["profile", "offline_access"])

    async def test_verifier_rejects_inactive_or_wrong_identity_tokens(
        self,
    ) -> None:
        config = _config()
        valid = {
            "object": "clerk_idp_oauth_access_token",
            "id": "oat_test",
            "client_id": "oauth_client",
            "subject": "user_owner",
            "scopes": ["profile"],
            "revoked": False,
            "revocation_reason": None,
            "expired": False,
            "expiration": 1_800_000_000,
            "created_at": 1_700_000_000,
            "updated_at": 1_700_000_000,
        }
        cases = {
            "inactive": {"active": False},
            "malformed client": {**valid, "client_id": None},
            "revoked": {**valid, "revoked": True},
            "expired": {**valid, "expired": True},
            "malformed scopes": {**valid, "scopes": "profile"},
        }

        for name, response_body in cases.items():
            with self.subTest(name):
                transport = httpx.MockTransport(
                    lambda request, body=response_body: httpx.Response(
                        200,
                        json=body,
                    )
                )
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url=config.clerk_api_url,
                ) as client:
                    verifier = ClerkApiTokenVerifier(
                        config,
                        http_client=client,
                    )
                    self.assertIsNone(
                        await verifier.verify_token("oauth-access-token")
                    )

    async def test_verifier_fails_closed_when_clerk_rejects_token(self) -> None:
        config = _config()
        transport = httpx.MockTransport(
            lambda request: httpx.Response(401, json={"errors": []})
        )

        async with httpx.AsyncClient(
            transport=transport,
            base_url=config.clerk_api_url,
        ) as client:
            verifier = ClerkApiTokenVerifier(config, http_client=client)
            self.assertIsNone(
                await verifier.verify_token("rejected-oauth-access-token")
            )

    def test_project_env_loads_remote_server_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "GARMIN_COACH_MCP_PUBLIC_URL=http://127.0.0.1:8765/mcp",
                        "GARMIN_COACH_CLERK_ISSUER=https://clerk.example.test",
                        "GARMIN_COACH_CLERK_SECRET_KEY=sk_test_secret",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                load_project_env(env_path)
                config = RemoteMcpConfig.from_env()

        self.assertEqual(
            config.public_url,
            "http://127.0.0.1:8765/mcp",
        )
        self.assertNotIn("sk_test_secret", repr(config))

    def test_factory_rejects_non_https_public_resource_outside_loopback(
        self,
    ) -> None:
        config = _config(public_url="http://mcp.example.test/mcp")

        with self.assertRaisesRegex(ValueError, "must use HTTPS outside loopback"):
            create_remote_server(
                config,
                token_verifier=_RejectingTokenVerifier(),
            )

if __name__ == "__main__":
    unittest.main()
