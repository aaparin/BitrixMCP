from __future__ import annotations

import unittest

import httpx

from bitrix_mcp.agent import parse_openrouter_directive, should_retry_with_cloud
from bitrix_mcp.bitrix import BitrixClient, ReadOnlyViolation, is_read_only_method
from bitrix_mcp.cache import TTLCache


class ReadOnlyMethodTests(unittest.TestCase):
    def test_allows_common_read_methods(self) -> None:
        self.assertTrue(is_read_only_method('user.get'))
        self.assertTrue(is_read_only_method('tasks.task.list'))
        self.assertTrue(is_read_only_method('crm.deal.fields'))

    def test_blocks_write_methods(self) -> None:
        self.assertFalse(is_read_only_method('crm.deal.add'))
        self.assertFalse(is_read_only_method('tasks.task.update'))
        self.assertFalse(is_read_only_method('batch'))


class TTLCacheTests(unittest.TestCase):
    def test_stores_and_reads_value(self) -> None:
        cache = TTLCache(ttl_seconds=60)
        key = cache.make_key('docs', {'q': 'user.get'})
        cache.set(key, {'ok': True})
        self.assertEqual(cache.get(key), {'ok': True})


class AgentFallbackTests(unittest.TestCase):
    def test_parses_openrouter_directive(self) -> None:
        question, force_openrouter = parse_openrouter_directive('use openrouter: who is responsible?')
        self.assertEqual(question, 'who is responsible?')
        self.assertTrue(force_openrouter)

    def test_plain_question_does_not_force_openrouter(self) -> None:
        question, force_openrouter = parse_openrouter_directive('who is responsible?')
        self.assertEqual(question, 'who is responsible?')
        self.assertFalse(force_openrouter)

    def test_weak_english_answer_triggers_fallback(self) -> None:
        self.assertTrue(should_retry_with_cloud('I need more information to answer this.'))


class BitrixClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_result_field(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), 'https://example.bitrix24.ru/rest/1/token/user.get.json')
            return httpx.Response(200, json={'result': [{'ID': '1'}]})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        result = await client.call_method('user.get', {'select': ['ID']})
        self.assertEqual(result, [{'ID': '1'}])

    async def test_adds_contact_responsible_field_to_contact_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn('ASSIGNED_BY_ID', request.read().decode())
            return httpx.Response(200, json={'result': []})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        result = await client.call_method('crm.contact.list', {'select': ['ID', 'NAME']})
        self.assertEqual(result, [])

    async def test_normalizes_company_list_name_to_title(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn('"=TITLE":"Dumbo - 1"', body)
            self.assertNotIn('"NAME"', body)
            self.assertIn('"TITLE"', body)
            self.assertIn('"ASSIGNED_BY_ID"', body)
            return httpx.Response(200, json={'result': []})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        result = await client.call_method(
            'crm.company.list',
            {'filter': {'NAME': 'Dumbo - 1'}, 'select': ['ID', 'NAME', 'ASSIGNED_BY_ID']},
        )
        self.assertEqual(result, [])

    async def test_normalizes_uppercase_company_list_params(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn('"filter":{"=TITLE":"Dumbo - 1"}', body)
            self.assertIn('"select":["ID","TITLE","ASSIGNED_BY_ID"]', body)
            self.assertNotIn('"FILTER"', body)
            self.assertNotIn('"SELECT"', body)
            return httpx.Response(200, json={'result': []})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        result = await client.call_method(
            'crm.company.list',
            {'FILTER': {'NAME': 'Dumbo - 1'}, 'SELECT': ['ID']},
        )
        self.assertEqual(result, [])

    async def test_normalizes_user_get_string_filter(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn('"filter":{"ID":456}', body)
            self.assertIn('"LAST_NAME"', body)
            self.assertIn('"EMAIL"', body)
            return httpx.Response(200, json={'result': []})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        result = await client.call_method(
            'user.get',
            {'filter': 'ID=456', 'select': ['ID', 'NAME']},
        )
        self.assertEqual(result, [])

    async def test_normalizes_user_get_user_id_param(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn('"filter":{"ID":4}', body)
            self.assertNotIn('"USER_ID"', body)
            return httpx.Response(200, json={'result': [{'ID': '4'}]})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        result = await client.call_method(
            'user.get',
            {'USER_ID': 4},
        )
        self.assertEqual(result, [{'ID': '4'}])

    async def test_count_list_method_uses_top_level_total(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn('"ID"', body)
            self.assertIn('"start":0', body)
            return httpx.Response(200, json={'result': [{'ID': '1'}], 'total': 123})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        total = await client.count_list_method('crm.company.list')
        self.assertEqual(total, 123)

    async def test_count_list_method_uses_nested_total(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={'result': {'tasks': [{'ID': '1'}], 'total': 42}})

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        total = await client.count_list_method('tasks.task.list')
        self.assertEqual(total, 42)

    async def test_blocks_write_method_before_http_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError('HTTP should not be called')

        client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaises(ReadOnlyViolation):
            await client.call_method('crm.deal.add', {'fields': {'TITLE': 'test'}})


if __name__ == '__main__':
    unittest.main()
