from base64 import b64encode
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Blueprint
import requests

from manga_kit import create_app
from manga_kit.config import Settings
from manga_kit.features import Feature, built_in_features
from manga_kit.komga import KomgaClient, KomgaError
from manga_kit.sample import create_sample_library


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(data_root=Path(temp.name) / 'state')
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.app.testing = True
        self.client = self.app.test_client()

    def test_pages_and_static_assets_render_without_external_calls(self):
        with patch('requests.Session.get', side_effect=AssertionError('Unexpected network request')):
            for path in ['/', '/library', '/organize', '/metadata', '/settings', '/roadmap',
                         '/static/app.css', '/static/tokens.css', '/static/app.js', '/static/theme.js', '/static/mark.svg']:
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    response.close()
        self.assertEqual(self.client.get('/healthz').json['status'], 'ok')

    def test_library_filters_by_kana_status_and_title(self):
        response = self.client.get('/library?q=ｶﾓﾒ&status=completed&row=か')
        html = response.get_data(as_text=True)
        self.assertIn('かもめ便り', html)
        self.assertNotIn('雨あがりの喫茶室', html)
        self.assertEqual(self.client.get('/api/library').json['stats']['books'], 22)

    def test_preview_uses_overrides_without_persisting(self):
        response = self.client.post('/api/organize/preview', json={'layout': {'completed': '{series}'}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['preview_only'])
        self.assertEqual(response.json['counts']['blocked'], 0)
        self.assertTrue(any(item['target'].startswith('[海野あお]') for item in response.json['items']))
        self.assertEqual(self.app.config['MANGA_KIT'].layout.completed, '{status}/{kana_row}/{series}')

    def test_invalid_preview_and_cross_site_requests_are_rejected(self):
        for payload in [{'layout': {'completed': '../{series}'}}, {'layout': {'bad': 'value'}}, []]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post('/api/organize/preview', json=payload).status_code, 400)
        self.assertEqual(self.client.post('/api/organize/preview', data='no', content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post('/api/library/refresh', headers={'Origin': 'https://other.test'}).status_code, 403)

    def test_configuration_response_never_contains_secret(self):
        with patch.dict(os.environ, {'KOMGA_API_KEY': 'a-secret-that-must-not-appear'}):
            response = self.client.get('/api/settings')
            self.assertTrue(response.json['api_key_configured'])
            self.assertNotIn('a-secret-that-must-not-appear', response.get_data(as_text=True))
            self.assertNotIn('a-secret-that-must-not-appear', self.client.get('/settings').get_data(as_text=True))

    def test_unconfigured_connection_is_an_honest_error(self):
        response = self.client.post('/api/komga/test', json={})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json['connected'])
        self.assertIn('未設定', response.json['error'])

    def test_optional_basic_auth_and_health_exemption(self):
        with patch.dict(os.environ, {'MANGA_KIT_ADMIN_TOKEN': 'test-password'}):
            client = create_app(settings=self.settings).test_client()
        self.assertEqual(client.get('/').status_code, 401)
        self.assertEqual(client.get('/api/library').status_code, 401)
        self.assertEqual(client.get('/healthz').status_code, 200)
        header = {'Authorization': 'Basic ' + b64encode(b'admin:test-password').decode()}
        self.assertEqual(client.get('/', headers=header).status_code, 200)
        bad_header = {'Authorization': 'Basic ' + b64encode(b'admin:wrong').decode()}
        self.assertEqual(client.get('/', headers=bad_header).status_code, 401)

    def test_factory_instances_do_not_share_library_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = replace(self.settings, demo=False, library_root=Path(tmp))
            create_sample_library(config.library_root, config.layout)
            other = create_app(settings=config)
            self.assertFalse(other.test_client().get('/api/library').json['demo'])
            self.assertTrue(self.client.get('/api/library').json['demo'])
            self.assertIsNot(self.app.extensions['library'], other.extensions['library'])

    def test_custom_feature_is_registered_in_navigation(self):
        notes = Blueprint('notes', __name__)
        notes.add_url_rule('/notes', endpoint='index', view_func=lambda: 'notes')
        feature = Feature('notes', 'メモ', 'notes.index', 'book', notes)
        client = create_app(settings=self.settings, features=[*built_in_features(), feature]).test_client()
        self.assertEqual(client.get('/notes').data, b'notes')
        self.assertIn('href="/notes"', client.get('/').get_data(as_text=True))

    def test_series_queries_are_bounded_and_strip_private_paths(self):
        self.app.extensions['komga'] = Mock()
        self.app.extensions['komga'].series.return_value = {'content': [{'id': 'id', 'name': 'Title',
                                                                     'url': '/private/path', 'booksCount': 3}], 'totalElements': 1}
        response = self.client.get('/api/komga/series?size=10')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('/private/path', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/komga/series?size=101').status_code, 400)

    def test_headers_and_unknown_pages(self):
        response = self.client.get('/')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/unknown').status_code, 404)
        self.assertEqual(self.client.get('/api/unknown').json['status'], 404)


class KomgaTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'KOMGA_API_KEY': 'mock-api-key'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.session = Mock()
        self.config = Settings(komga_url='https://komga.test/komga')
        self.client = KomgaClient(self.config, self.session)

    def response(self, data, status=200):
        result = Mock(status_code=status)
        result.json.return_value = data
        self.session.get.return_value = result
        return result

    def test_api_key_header_base_path_and_timeout(self):
        self.response([{'id': '1', 'name': '漫画'}])
        result = self.client.connection()
        self.assertTrue(result['connected'])
        self.assertEqual(result['library_count'], 1)
        args, kwargs = self.session.get.call_args
        self.assertEqual(args[0], 'https://komga.test/komga/api/v1/libraries')
        self.assertEqual(kwargs['headers']['X-API-Key'], 'mock-api-key')
        self.assertEqual(kwargs['timeout'], 10)
        self.assertFalse(kwargs['allow_redirects'])

    def test_authentication_redirect_timeout_and_payload_errors(self):
        for status in [401, 403, 302]:
            self.response([], status)
            with self.subTest(status=status), self.assertRaises(KomgaError):
                self.client.connection()
        self.session.get.side_effect = requests.Timeout('contains secret')
        with self.assertRaises(KomgaError) as caught:
            self.client.connection()
        self.assertNotIn('secret', str(caught.exception))
        self.session.get.side_effect = None
        self.response({'unexpected': 'shape'})
        with self.assertRaises(KomgaError):
            self.client.connection()

    def test_missing_key_never_sends_a_request(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(KomgaError):
            self.client.connection()
        self.session.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
