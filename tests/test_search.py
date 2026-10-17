# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import app
import qq_api


class SearchTests(unittest.TestCase):
    def test_missing_login_does_not_contact_upstream(self):
        with mock.patch.object(qq_api, 'get_cookie', return_value=''), \
                mock.patch.object(qq_api, '_post_musicu') as upstream:
            response = app.app.test_client().get('/api/search?kw=周深')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['code'], 'LOGIN_REQUIRED')
        upstream.assert_not_called()

    def test_business_error_is_not_reported_as_empty_results(self):
        payload = {'code': 0, 'req_1': {'code': 2000, 'data': {}}}
        with mock.patch.object(qq_api, 'get_cookie', return_value='test-only'), \
                mock.patch.object(qq_api, '_apply_cookie'), \
                mock.patch.object(qq_api, '_post_musicu', return_value=payload):
            response = app.app.test_client().get('/api/search?kw=周深')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()['code'], 'SEARCH_REJECTED')

    def test_successful_search_keeps_song_fields(self):
        payload = {'code': 0, 'req_1': {'code': 0, 'data': {'body': {'song': {'list': [
            {'name': '示例', 'mid': 'song-mid', 'singer': [{'name': '歌手'}],
             'file': {'media_mid': 'audio-mid'}}
        ]}}}}}
        with mock.patch.object(qq_api, 'get_cookie', return_value='test-only'), \
                mock.patch.object(qq_api, '_apply_cookie'), \
                mock.patch.object(qq_api, '_post_musicu', return_value=payload):
            songs = qq_api.search('示例')
        self.assertEqual(songs[0]['mid'], 'song-mid')
        self.assertEqual(songs[0]['file']['media_mid'], 'audio-mid')

    def test_valid_empty_response_is_distinct_from_platform_error(self):
        payload = {'code': 0, 'req_1': {'code': 0, 'data': {'body': {'song': {'list': []}}}}}
        with mock.patch.object(qq_api, 'get_cookie', return_value='test-only'), \
                mock.patch.object(qq_api, '_apply_cookie'), \
                mock.patch.object(qq_api, '_post_musicu', return_value=payload), \
                mock.patch.object(qq_api.time, 'sleep'):
            self.assertEqual(qq_api.search('不存在的关键词'), [])

    def test_network_error_does_not_echo_secrets(self):
        with mock.patch.object(qq_api, 'get_cookie', return_value='test-only'), \
                mock.patch.object(qq_api, '_apply_cookie'), \
                mock.patch.object(qq_api, '_post_musicu', side_effect=RuntimeError('secret-token')), \
                mock.patch.object(qq_api.time, 'sleep'):
            with self.assertRaises(qq_api.SearchError) as caught:
                qq_api.search('示例')
        self.assertNotIn('secret-token', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
