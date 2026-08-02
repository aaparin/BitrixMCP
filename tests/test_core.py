from __future__ import annotations

import unittest

import httpx

from bitrix_mcp.bitrix import BitrixClient, ReadOnlyViolation
from bitrix_mcp.cache import TTLCache
from bitrix_mcp.crm_metadata import CrmMetadataResolver
from bitrix_mcp.crm_statuses import CRM_STATUSES_CACHE
from bitrix_mcp.direct_tools import normalize_enum_items, parse_object_input, parse_object_list_input, parse_string_list_input
from bitrix_mcp.people_tools import build_user_search_filters, dedupe_users, user_matches_query
from bitrix_mcp.read_tools import (
    crm_items_list_data,
    normalize_task_status_filter,
    resolve_crm_entity_type_id,
    tasks_list_data,
    telephony_calls_list_data,
)
from bitrix_mcp.server import StaticBearerTokenVerifier


class TTLCacheTests(unittest.TestCase):
    def test_stores_and_reads_value(self) -> None:
        cache = TTLCache(ttl_seconds=60)
        key = cache.make_key('docs', {'q': 'user.get'})
        cache.set(key, {'ok': True})
        self.assertEqual(cache.get(key), {'ok': True})

    def test_scoped_keys_differ(self) -> None:
        cache = TTLCache(ttl_seconds=60)
        a = cache.make_key('fields', {}, scope='portal-a')
        b = cache.make_key('fields', {}, scope='portal-b')
        self.assertNotEqual(a, b)


class DirectToolHelperTests(unittest.TestCase):
    def test_parses_direct_rest_params_json_string(self) -> None:
        self.assertEqual(parse_object_input('{"filter": {"ID": 1}}', name='params'), {'filter': {'ID': 1}})

    def test_parses_string_list_json_or_csv(self) -> None:
        self.assertEqual(parse_string_list_input('["LEAD", "DEAL"]', name='entities'), ['LEAD', 'DEAL'])
        self.assertEqual(parse_string_list_input('LEAD, DEAL', name='entities'), ['LEAD', 'DEAL'])

    def test_parses_object_list_json_string(self) -> None:
        self.assertEqual(
            parse_object_list_input('[{"field": "status", "value": "won"}]', name='conditions'),
            [{'field': 'status', 'value': 'won'}],
        )

    def test_normalizes_enum_items(self) -> None:
        self.assertEqual(
            normalize_enum_items([{'ID': '1', 'VALUE': 'Active', 'XML_ID': 'A', 'EXTRA': 'ignore'}]),
            [{'ID': '1', 'VALUE': 'Active', 'XML_ID': 'A'}],
        )

    def test_resolves_static_and_dynamic_crm_entity_types(self) -> None:
        self.assertEqual(resolve_crm_entity_type_id('deal'), 2)
        self.assertEqual(resolve_crm_entity_type_id('CRM_COMPANIES'), 4)
        self.assertEqual(resolve_crm_entity_type_id('DYNAMIC_1032'), 1032)
        self.assertEqual(resolve_crm_entity_type_id(1036), 1036)


class PeopleToolHelperTests(unittest.TestCase):
    def test_builds_user_search_filters_from_name(self) -> None:
        self.assertEqual(
            build_user_search_filters('Alex Minaev'),
            [
                {'NAME': 'Alex', 'LAST_NAME': 'Minaev'},
                {'NAME': 'Minaev', 'LAST_NAME': 'Alex'},
                {'NAME': 'Alex'},
                {'LAST_NAME': 'Minaev'},
            ],
        )

    def test_matches_user_query_by_display_name(self) -> None:
        self.assertTrue(user_matches_query({'NAME': 'Aleksandrs', 'LAST_NAME': 'Minajevs'}, 'aleksandrs minajevs'))
        self.assertFalse(user_matches_query({'NAME': 'Irina', 'LAST_NAME': 'Ostapko'}, 'alex minaev'))

    def test_dedupes_users_by_id(self) -> None:
        self.assertEqual(
            dedupe_users([{'ID': '1', 'NAME': 'A'}, {'ID': '1', 'NAME': 'A'}, {'ID': '2', 'NAME': 'B'}]),
            [{'ID': '1', 'NAME': 'A'}, {'ID': '2', 'NAME': 'B'}],
        )

    def test_normalizes_open_task_status(self) -> None:
        self.assertEqual(normalize_task_status_filter('open'), {'!STATUS': 5})
        self.assertEqual(normalize_task_status_filter('completed'), {'STATUS': 5})
        self.assertEqual(normalize_task_status_filter(None), {})


class StaticBearerTokenVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_expected_token(self) -> None:
        verifier = StaticBearerTokenVerifier('secret')
        token = await verifier.verify_token('secret')
        self.assertIsNotNone(token)
        self.assertEqual(token.client_id, 'static-bearer-token')

    async def test_rejects_wrong_token(self) -> None:
        verifier = StaticBearerTokenVerifier('secret')
        self.assertIsNone(await verifier.verify_token('wrong'))


class BitrixClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for name in list(self.__dict__):
            value = getattr(self, name)
            if isinstance(value, BitrixClient):
                await value.aclose()

    async def test_returns_result_field(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), 'https://example.bitrix24.ru/rest/1/token/user.get.json')
            return httpx.Response(200, json={'result': [{'ID': '1'}]})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        result = await self.client.call_method('user.get', {'select': ['ID']})
        self.assertEqual(result, [{'ID': '1'}])

    async def test_adds_contact_responsible_field_to_contact_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn('ASSIGNED_BY_ID', request.read().decode())
            return httpx.Response(200, json={'result': []})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        result = await self.client.call_method('crm.contact.list', {'select': ['ID', 'NAME']})
        self.assertEqual(result, [])

    async def test_normalizes_company_list_name_to_title(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn('"=TITLE":"Dumbo - 1"', body)
            self.assertNotIn('"NAME"', body)
            self.assertIn('"TITLE"', body)
            self.assertIn('"ASSIGNED_BY_ID"', body)
            return httpx.Response(200, json={'result': []})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        result = await self.client.call_method(
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

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        result = await self.client.call_method(
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

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        result = await self.client.call_method(
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

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        result = await self.client.call_method('user.get', {'USER_ID': 4})
        self.assertEqual(result, [{'ID': '4'}])

    async def test_count_list_method_uses_top_level_total(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn('"ID"', body)
            self.assertIn('"start":0', body)
            return httpx.Response(200, json={'result': [{'ID': '1'}], 'total': 123})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        total = await self.client.count_list_method('crm.company.list')
        self.assertEqual(total, 123)

    async def test_count_list_method_uses_nested_total(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={'result': {'tasks': [{'ID': '1'}], 'total': 42}})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        total = await self.client.count_list_method('tasks.task.list')
        self.assertEqual(total, 42)

    async def test_blocks_write_method_before_http_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError('HTTP should not be called')

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(ReadOnlyViolation):
            await self.client.call_method('crm.deal.add', {'fields': {'TITLE': 'test'}})

    async def test_allows_methods_discovery_endpoint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith('/methods.json'))
            return httpx.Response(200, json={'result': ['user.get']})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        result = await self.client.call_method('methods', {})
        self.assertEqual(result, ['user.get'])

    async def test_crm_items_list_uses_universal_read_method(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith('/crm.item.list.json'))
            body = request.read().decode()
            self.assertIn('"entityTypeId":2', body)
            self.assertIn('"useOriginalUfNames":"Y"', body)
            self.assertIn('"filter":{"stageSemanticId":"S"}', body)
            return httpx.Response(200, json={'result': {'items': []}, 'total': 0})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        payload = await crm_items_list_data(self.client, 'deal', filter={'stageSemanticId': 'S'})
        self.assertEqual(payload['total'], 0)

    async def test_tasks_list_keeps_pagination_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith('/tasks.task.list.json'))
            body = request.read().decode()
            self.assertIn('"RESPONSIBLE_ID":7', body)
            self.assertIn('"DEADLINE"', body)
            return httpx.Response(
                200,
                json={'result': {'tasks': [{'ID': '1'}], 'total': 25}, 'next': 50},
            )

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        payload = await tasks_list_data(self.client, filter={'RESPONSIBLE_ID': 7})
        self.assertEqual(payload['result']['total'], 25)
        self.assertEqual(payload['next'], 50)

    async def test_telephony_call_list_preserves_required_uppercase_params(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith('/voximplant.statistic.get.json'))
            body = request.read().decode()
            self.assertIn('"FILTER":{">=CALL_START_DATE":"2026-01-01"}', body)
            self.assertIn('"SORT":"CALL_START_DATE"', body)
            self.assertIn('"ORDER":"DESC"', body)
            self.assertIn('"start":50', body)
            return httpx.Response(200, json={'result': [], 'total': 0})

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        payload = await telephony_calls_list_data(
            self.client,
            filter={'>=CALL_START_DATE': '2026-01-01'},
            start=50,
        )
        self.assertEqual(payload['total'], 0)


class CrmMetadataResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cache = TTLCache(ttl_seconds=60)
        CRM_STATUSES_CACHE.clear()

    async def asyncTearDown(self) -> None:
        if hasattr(self, 'client'):
            await self.client.aclose()

    async def test_resolves_deal_count_filter_aliases(self) -> None:
        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(lambda request: httpx.Response(500, json={'error': 'unexpected'})),
        )
        resolver = CrmMetadataResolver(self.client, cache=self.cache)
        result = await resolver.resolve_filter('deal', filter={'status': 'won', 'year': 2026})
        self.assertEqual(
            result,
            {
                'STAGE_SEMANTIC_ID': 'S',
                '>=CLOSEDATE': '2026-01-01',
                '<CLOSEDATE': '2027-01-01',
            },
        )

    async def test_resolves_bad_stage_id_won_to_deal_semantic(self) -> None:
        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(lambda request: httpx.Response(500, json={'error': 'unexpected'})),
        )
        resolver = CrmMetadataResolver(self.client, cache=self.cache)
        result = await resolver.resolve_filter(
            'deal',
            filter={'=STAGE_ID': 'WON', '>=CLOSE_DATE': '2026-01-01', '<=CLOSE_DATE': '2026-12-31'},
        )
        self.assertEqual(
            result,
            {
                'STAGE_SEMANTIC_ID': 'S',
                '>=CLOSEDATE': '2026-01-01',
                '<=CLOSEDATE': '2026-12-31',
            },
        )

    async def test_resolves_company_status_dictionary_value(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith('/crm.company.fields.json'):
                return httpx.Response(
                    200,
                    json={'result': {'COMPANY_TYPE': {'type': 'crm_status', 'statusType': 'COMPANY_TYPE', 'title': 'Company type'}}},
                )
            if url.endswith('/crm.status.entity.types.json'):
                return httpx.Response(200, json={'result': [{'ID': 'COMPANY_TYPE', 'ENTITY_TYPE_ID': 4}]})
            if url.endswith('/crm.status.list.json'):
                body = request.read().decode()
                self.assertIn('"ENTITY_ID":"COMPANY_TYPE"', body)
                return httpx.Response(200, json={'result': [{'STATUS_ID': 'CUSTOMER', 'NAME': 'Customer'}]})
            raise AssertionError(f'Unexpected URL: {url}')

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        resolver = CrmMetadataResolver(self.client, cache=self.cache)
        result = await resolver.resolve_filter('company', conditions=[{'field': 'type', 'value': 'Customer'}])
        self.assertEqual(result, {'COMPANY_TYPE': 'CUSTOMER'})

    async def test_resolves_status_field_by_value_when_field_hint_is_generic(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith('/crm.company.fields.json'):
                return httpx.Response(
                    200,
                    json={
                        'result': {
                            'COMPANY_TYPE': {'type': 'crm_status', 'statusType': 'COMPANY_TYPE', 'title': 'Company type'},
                            'INDUSTRY': {'type': 'crm_status', 'statusType': 'INDUSTRY', 'title': 'Industry'},
                        }
                    },
                )
            if url.endswith('/crm.status.entity.types.json'):
                return httpx.Response(
                    200,
                    json={'result': [{'ID': 'COMPANY_TYPE', 'ENTITY_TYPE_ID': 4}, {'ID': 'INDUSTRY', 'ENTITY_TYPE_ID': 4}]},
                )
            if url.endswith('/crm.status.list.json'):
                body = request.read().decode()
                if '"ENTITY_ID":"COMPANY_TYPE"' in body:
                    return httpx.Response(200, json={'result': [{'STATUS_ID': 'ACTIVE', 'NAME': 'Active'}]})
                if '"ENTITY_ID":"INDUSTRY"' in body:
                    return httpx.Response(200, json={'result': [{'STATUS_ID': 'IT', 'NAME': 'IT'}]})
            raise AssertionError(f'Unexpected URL: {url}')

        self.client = BitrixClient(
            'https://example.bitrix24.ru/rest/1/token/',
            transport=httpx.MockTransport(handler),
        )
        resolver = CrmMetadataResolver(self.client, cache=self.cache)
        result = await resolver.resolve_filter('company', conditions=[{'field': 'status', 'value': 'Active'}])
        self.assertEqual(result, {'COMPANY_TYPE': 'ACTIVE'})


if __name__ == '__main__':
    unittest.main()
