from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken

from bitrix_mcp.oauth.provider import (
    BitrixOAuthError,
    BitrixOAuthProvider,
    BitrixTokenClient,
    BitrixTokenVerifier,
    normalize_token_response,
)
from bitrix_mcp.oauth.resolver import IdentityResolver, identity_from_access_token
from bitrix_mcp.oauth.store import TokenStore

TOKEN_RESPONSE = {
    'access_token': 'AT',
    'refresh_token': 'RT',
    'expires_in': 3600,
    'expires': 0,
    'member_id': 'm1',
    'user_id': 7,
    'client_endpoint': 'https://ex.bitrix24.ru/rest/',
    'scope': 'crm,task,user',
}


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TokenStore(Path(self._tmp.name) / 'tokens.db')

    def tearDown(self) -> None:
        self._tmp.cleanup()


class BitrixTokenClientTests(StoreTestCase):
    def _client(self, handler) -> BitrixTokenClient:
        return BitrixTokenClient(
            client_id='local.x',
            client_secret='secret',
            store=self.store,
            transport=httpx.MockTransport(handler),
        )

    async def test_exchanges_code_with_get_and_pins_portal(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=TOKEN_RESPONSE)

        token = await self._client(handler).fetch_token('ignored', code='CODE', redirect_uri='x')

        self.assertEqual(seen[0].method, 'GET')
        self.assertEqual(
            dict(seen[0].url.params),
            {'client_id': 'local.x', 'client_secret': 'secret', 'grant_type': 'authorization_code', 'code': 'CODE'},
        )
        self.assertEqual(token['access_token'], 'AT')
        self.assertNotIn('scope', token)
        self.assertEqual(token['bitrix_scope'], 'crm,task,user')
        self.assertEqual(self.store.get_pinned_member_id(), 'm1')

    async def test_refresh_rejects_other_portal(self) -> None:
        self.store.pin_or_check_member_id('m1')

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params['grant_type'], 'refresh_token')
            return httpx.Response(200, json={**TOKEN_RESPONSE, 'member_id': 'other'})

        with self.assertRaises(BitrixOAuthError):
            await self._client(handler).refresh_token('ignored', refresh_token='RT')

    async def test_raises_on_oauth_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={'error': 'invalid_grant', 'error_description': 'expired'})

        with self.assertRaisesRegex(BitrixOAuthError, 'invalid_grant'):
            await self._client(handler).fetch_token(code='CODE')

    def test_normalize_derives_expires_in(self) -> None:
        token = normalize_token_response({'access_token': 'a', 'expires': 4102444800})
        self.assertGreater(token['expires_in'], 3600)
        self.assertEqual(token['token_type'], 'Bearer')


class BitrixTokenVerifierTests(StoreTestCase):
    async def test_valid_token_exposes_bitrix_user(self) -> None:
        self.store.pin_or_check_member_id('m1')
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(str(request.url).split('?')[0], 'https://ex.bitrix24.ru/rest/user.current.json')
            self.assertEqual(request.url.params['auth'], 'AT')
            return httpx.Response(200, json={'result': {'ID': '7', 'EMAIL': 'Ivan@Ex.ru', 'NAME': 'Ivan', 'LAST_NAME': 'P'}})

        verifier = BitrixTokenVerifier(
            portal_url='https://ex.bitrix24.ru/',
            store=self.store,
            transport=httpx.MockTransport(handler),
        )
        token = await verifier.verify_token('AT')
        await verifier.verify_token('AT')

        self.assertIsNotNone(token)
        self.assertEqual(calls, 1)
        self.assertEqual(
            token.claims,
            {
                'bitrix_user_id': 7,
                'email': 'ivan@ex.ru',
                'name': 'Ivan P',
                'member_id': 'm1',
                'client_endpoint': 'https://ex.bitrix24.ru/rest/',
            },
        )

    async def test_expired_token_is_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={'error': 'expired_token'})

        verifier = BitrixTokenVerifier(portal_url='https://ex.bitrix24.ru', transport=httpx.MockTransport(handler))
        self.assertIsNone(await verifier.verify_token('AT'))


class IdentityResolutionTests(unittest.TestCase):
    def _access_token(self) -> AccessToken:
        return AccessToken(
            token='AT',
            client_id='7',
            scopes=[],
            claims={
                'bitrix_user_id': 7,
                'email': 'ivan@ex.ru',
                'name': 'Ivan P',
                'member_id': 'm1',
                'client_endpoint': 'https://ex.bitrix24.ru/rest/',
            },
        )

    def test_builds_oauth_identity_from_claims(self) -> None:
        identity = identity_from_access_token(self._access_token())
        self.assertIsNotNone(identity)
        self.assertEqual(identity.bitrix_user_id, 7)
        self.assertEqual(identity.auth_params(), {'auth': 'AT'})
        self.assertEqual(identity.request_url('user.current'), 'https://ex.bitrix24.ru/rest/user.current.json')

    def test_resolver_requires_user_token_in_oauth_mode(self) -> None:
        resolver = IdentityResolver(webhook_url='', oauth_enabled=True)
        with self.assertRaisesRegex(RuntimeError, 'authentication is required'):
            resolver.resolve(None)
        self.assertEqual(resolver.resolve(self._access_token()).mode, 'oauth')

    def test_resolver_ignores_token_when_oauth_disabled(self) -> None:
        resolver = IdentityResolver(webhook_url='https://ex.bitrix24.ru/rest/1/abc/', oauth_enabled=False)
        self.assertEqual(resolver.resolve(self._access_token()).mode, 'webhook')


class BitrixOAuthProviderTests(StoreTestCase):
    def test_upstream_authorize_url_targets_portal_without_pkce(self) -> None:
        provider = BitrixOAuthProvider(
            portal_url='https://ex.bitrix24.ru',
            client_id='local.x',
            client_secret='secret',
            base_url='https://mcp.example.com',
            store=self.store,
            storage_dir=Path(self._tmp.name) / 'oauth-proxy',
            storage_encryption_key=Fernet.generate_key().decode(),
        )
        url = provider._build_upstream_authorize_url(  # noqa: SLF001
            'txn',
            {'scopes': [], 'resource': 'https://mcp.example.com/mcp'},
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(f'{parsed.scheme}://{parsed.netloc}{parsed.path}', 'https://ex.bitrix24.ru/oauth/authorize/')
        self.assertEqual(query['redirect_uri'], ['https://mcp.example.com/oauth/callback'])
        self.assertEqual(query['state'], ['txn'])
        self.assertNotIn('code_challenge', query)
        self.assertNotIn('resource', query)


if __name__ == '__main__':
    unittest.main()
