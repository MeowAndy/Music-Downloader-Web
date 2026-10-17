"""Offline public-fixture parsing and fixed-origin transport regression tests."""
import contextlib
import hashlib
import http.client
import importlib.util
import io
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hifiti_api as api

FIXTURES = ROOT / 'tests' / 'fixtures' / 'hifiti'


# Entirely synthetic authorized HTML: no real account, cookies or share links.
SYNTHETIC_AUTHORIZED_THREAD = '''<script>var uid = 42;</script><h1>Synthetic authorized response</h1>
<div id="thread-content" class="message" isfirst="1">
<div class="hide-content"><p>FLAC <a href="https://pan.baidu.com/s/1GuiFixture?pwd=ab12">Baidu fixture</a> 提取码：ab12</p>
<p>MP3 https://pan.quark.cn/s/GuiFixture 提取码：cd34</p></div>
<p><a href="https://example.com/file?sign=keep%2Bthis%2Fvalue&amp;expires=123">Signed fixture</a></p>
<div hidden>https://pan.baidu.com/s/HIDDEN 提取码：hh11</div>
<div style="display: none !important"><a href="https://example.com/DECOY">hidden</a></div>
<div style="visibility:hidden"><span>SECRET_HIDDEN</span></div>
<div aria-hidden="true"><a href="https://example.com/ARIA">hidden</a></div>
<input type="hidden" value="SECRET_INPUT"><input type="password" value="SECRET_PASSWORD">
<span data-session="SECRET_ATTRIBUTE">歌词中的密码、权限、积分和付费不是限制提示</span>
<script>var arbitrary = 'SECRET_SCRIPT';</script>
<a href="javascript:alert(1)">bad</a><a href="http://127.0.0.1/internal">bad</a>
<a href="https://example.com/?token=SECRET_TOKEN">bad</a></div>'''.encode('utf-8')


class HifitiParsingTests(unittest.TestCase):
    def parse_body(self, body):
        return api.parse_thread(('<h1>Synthetic</h1><div id="thread-content" class="message" isfirst="1">'
                                 + body + '</div>').encode(), '1')

    def test_synthetic_revealed_resources_codes_signed_url_and_hidden_decoys(self):
        data = api.parse_thread(SYNTHETIC_AUTHORIZED_THREAD, '1')
        self.assertEqual(['ab12', 'cd34', ''], [item['code'] for item in data['resources']])
        self.assertEqual('https://example.com/file?sign=keep%2Bthis%2Fvalue&expires=123', data['resources'][2]['url'])
        self.assertEqual([{'url': item['url'], 'label': item['label']} for item in data['resources']], data['links'])
        self.assertEqual('', data['notice'])
        for secret in ('SECRET', 'HIDDEN', 'DECOY', '/ARIA', '127.0.0.1', 'javascript:'):
            self.assertNotIn(secret, str(data))

    def test_nearby_separate_codes_shared_paragraph_br_and_next_paragraph(self):
        a, b = 'https://pan.quark.cn/s/first', 'https://pan.quark.cn/s/second'
        for body in (f'<p><a href="{a}">A</a> 提取码：aa11 <a href="{b}">B</a> 访问码：bb22</p>',
                     f'<p>{a} 提取码：aa11<br>{b} 密码：bb22</p>',
                     f'<p><a href="{a}">A</a></p><p>提取码：aa11</p><p><a href="{b}">B</a></p><p>访问码：bb22</p>'):
            with self.subTest(body=body):
                self.assertEqual(['aa11', 'bb22'], [x['code'] for x in self.parse_body(body)['resources']])

    def test_ambiguous_shared_conflicting_and_distant_codes_are_not_guessed(self):
        a, b = 'https://pan.quark.cn/s/first', 'https://pan.quark.cn/s/second'
        for body, expected in ((f'<p>{a} {b} 提取码：aa11</p>', ['', '']),
                               (f'<p>{a} 提取码：aa11 密码：bb22</p>', ['']),
                               (f'<p>{a}</p><p>' + '歌词' * 100 + '</p><p>提取码：aa11</p>', ['']),
                               (f'<p>{a}</p><p>{b}</p><p>提取码：aa11</p>', ['', 'aa11'])):
            with self.subTest(body=body):
                self.assertEqual(expected, [x['code'] for x in self.parse_body(body)['resources']])

    def test_provider_specific_query_fragment_and_unrelated_auth_secrets(self):
        urls = [('https://pan.baidu.com/s/one?pwd=aa11&sign=A%2BB%2FC', 'aa11'),
                ('https://pan.quark.cn/s/two#passcode=bb22', 'bb22'),
                ('https://pan.baidu.com/s/three#cc33', 'cc33'),
                ('https://example.com/file?pwd=dd44', ''),
                ('https://pan.baidu.com/not-share?pwd=ee55', ''),
                ('https://pan.baidu.com/s/conflict?pwd=aa11#pwd=bb22', '')]
        data = self.parse_body(''.join(f'<p><a href="{url}">Resource</a></p>' for url, _ in urls))
        self.assertEqual(urls, [(item['url'], item['code']) for item in data['resources']])
        for url in ('https://example.com/?%74oken=SECRET', 'https://example.com/#session=SECRET'):
            self.assertEqual('', api._safe_url(url))

    def test_readonly_and_explicit_copy_text_require_visible_resource_labels(self):
        data = self.parse_body('''<p>下载地址：<input readonly type="text" value="https://pan.quark.cn/s/input"></p>
        <p>提取码：<input readonly value="aa11"></p>
        <button data-clipboard-text="https://pan.quark.cn/s/copy">复制链接</button>
        <span data-copy-text="bb22">提取码</span>
        <p><input readonly value="https://example.com/NO_LABEL"></p>
        <p>下载地址<input value="https://example.com/EDITABLE"></p>
        <span data-clipboard-text="https://example.com/NO_LABEL_ATTRIBUTE">other</span>
        <span data-url="https://example.com/ARBITRARY">下载链接</span>
        <div hidden><button data-copy-text="https://example.com/HIDDEN">复制链接</button></div>
        <form>下载地址<input readonly value="https://example.com/FORM"></form>''')
        self.assertEqual([('https://pan.quark.cn/s/input', 'aa11'), ('https://pan.quark.cn/s/copy', 'bb22')],
                         [(x['url'], x['code']) for x in data['resources']])
        for secret in ('NO_LABEL', 'EDITABLE', 'ARBITRARY', 'HIDDEN', '/FORM'):
            self.assertNotIn(secret, str(data))

    def test_hidden_ancestor_and_lyric_words_do_not_create_restriction_notice(self):
        data = self.parse_body('<div class="hide-content">歌词：密码 权限 积分 付费 隐藏内容</div>')
        self.assertEqual('', data['notice'])
        hidden = b'<section aria-hidden="true"><div id="thread-content" class="message" isfirst="1">SECRET https://example.com/secret</div></section>'
        self.assertEqual([], api.parse_thread(hidden, '1')['resources'])
        self.assertNotIn('SECRET', api.parse_thread(hidden, '1')['text'])

    def test_extraction_resource_and_text_bounds(self):
        data = self.parse_body(''.join(f'<p>https://example.com/file/{i} 提取码：aa11</p>' for i in range(500)))
        self.assertEqual(100, len(data['resources']))
        self.assertLessEqual(len(data['text']), api.MAX_TEXT)

    def test_default_site_order_highlights_tags_and_metadata(self):
        data = api.parse_search((FIXTURES / 'search.html').read_bytes(), '周深 泪桥')
        self.assertEqual((24, 4566, 191, True), (len(data['items']), data['total'], data['pages'], data['has_next']))
        self.assertEqual(['104813', '68903', '173771'], [x['id'] for x in data['items'][:3]])
        first = data['items'][0]
        self.assertEqual('古巨基/周深《泪桥(Live)》[FLAC/MP3-320K]', first['title'])
        self.assertIn('泪桥 (live)', data['items'][1]['title'])
        self.assertEqual(('fixture-author-001', '9月前', '5天前', '544', ['[夸克网盘]']),
                         (first['author'], first['created'], first['last_reply'], first['views'], first['tags']))
        self.assertNotIn('<span', str(data))

    def test_page_two_and_exact_results_not_reordered(self):
        data = api.parse_search((FIXTURES / 'search-page2.html').read_bytes(), '周深 泪桥', 2)
        self.assertEqual(2, data['page'])
        self.assertEqual(['167791', '167786', '167770', '167052'], [x['id'] for x in data['items'][:4]])
        exact = api.parse_search((FIXTURES / 'search-exact.html').read_bytes(), '周深 泪桥')
        self.assertEqual(['104813', '68903'], [x['id'] for x in exact['items']])
        self.assertEqual((2, 1, False), (exact['total'], exact['pages'], exact['has_next']))

    def test_post_search_preserves_duplicates_excerpts_unknown_total(self):
        data = api.parse_search((FIXTURES / 'search-posts.html').read_bytes(), '周深 泪桥')
        self.assertEqual((23, None, 2009), (len(data['items']), data['total'], data['pages']))
        self.assertEqual('2019247', data['items'][0]['pid'])
        self.assertTrue(data['items'][0]['excerpt'].startswith('周深：'))
        self.assertNotIn('newpost', str(data))

    def test_thread_restricted_is_not_fake_full_content(self):
        data = api.parse_thread((FIXTURES / 'thread-104813.html').read_bytes(), '104813')
        self.assertEqual([], data['links'])
        self.assertIn('受限', data['notice'])
        self.assertIn('无心过问你的心里', data['text'])
        self.assertNotIn('user-login.htm', str(data))

    def test_hostile_html_is_plain_and_unsafe_links_are_excluded(self):
        source = '''<h1>A<script>SECRET</script><span> &amp; B</span></h1>
        <div id="thread-content" class="message" isfirst="1">
        <p>歌词中的权限不是锁</p><script>SECRET</script><iframe>SECRET</iframe>
        <form><a href="https://example.com/hidden">invisible</a></form>
        <a href="javascript:alert(1)">bad</a><a href="data:text/html,secret">bad</a>
        <a href="https://user:pass@example.com/x">bad</a><a href="http://127.0.0.1/x">bad</a>
        <a href="http://[::1]/x">bad</a><a href="http://2130706433/x">bad</a>
        <a href="http://example.local/x">bad</a><a href="https://example.com/?token=SECRET">bad</a>
        <a href="https://pan.example.com/s/abc?sign=keep%2Bthis&amp;pwd=abcd">资源</a></div>'''
        data = api.parse_thread(source.encode(), '1')
        self.assertEqual('', data['notice'])
        self.assertNotIn('SECRET', str(data))
        self.assertEqual([{'label': '资源', 'url': 'https://pan.example.com/s/abc?sign=keep%2Bthis&pwd=abcd'}], data['links'])
        self.assertEqual('A & B', data['title'])

    def test_external_urls_reject_browser_local_literals_and_account_secrets(self):
        for value in ('http://0x7f.0.0.1/x', 'http://0177.0.0.1/x', 'http://127.1/x',
                      'http://%31%32%37.0.0.1/x', 'http://foo.localhost/x',
                      'https://www.hifiti.com/x?bbs_token=SECRET',
                      'https://www.hifiti.com/x?bbs_sid=SECRET', '/user-login.htm'):
            self.assertEqual('', api._safe_url(value), value)

    def test_unknown_challenge_and_oversize_fail_closed(self):
        for raw, code in ((b'<html>Checking your browser</html>', 'CHALLENGE'),
                          (b'<html>unknown search structure</html>', 'UPSTREAM_ERROR'),
                          (b'x' * (api.MAX_BODY + 1), 'UPSTREAM_ERROR')):
            with self.subTest(code=code), self.assertRaises(api.ApiError) as ctx:
                api.parse_search(raw, 'x')
            self.assertEqual(code, ctx.exception.code)

    def test_identity_needs_top_level_positive_uid_and_header_self_menu(self):
        self.assertEqual('', api._identity(api._html((FIXTURES / 'search.html').read_bytes())))
        good = b'<script>var uid = 42; var gid = 1;</script><header id="header"><a href="user-42.htm">Alice</a><a href="user-logout.htm">Logout</a></header>'
        self.assertEqual('Alice', api._identity(api._html(good)))
        for raw in (good.replace(b'uid = 42', b'uid = 0'), good.replace(b'id="header"', b'id="post"'),
                    good.replace(b'user-42.htm', b'user-43.htm'), good.replace(b'user-logout.htm', b'user-login.htm')):
            self.assertEqual('', api._identity(api._html(raw)))

    def test_exact_official_md5_and_xn_route_encoding(self):
        self.assertEqual(hashlib.md5('密码abc'.encode()).hexdigest(), api._password_md5('密码abc'))
        self.assertEqual(api._password_md5('a\nb'), api._password_md5('a\r\nb'))
        self.assertEqual(hashlib.md5(b'\xed\xa0\xbd\xed\xb8\x80').hexdigest(), api._password_md5('😀'))
        self.assertEqual("A_2DB_5F_2E_7E_21_2A_28_29'_20_E6_B3_AA", api._xn_encode("A-B_.~!*()' 泪"))


class HifitiTransportTests(unittest.TestCase):
    def test_import_has_no_network(self):
        spec = importlib.util.spec_from_file_location('hifiti_no_network_fixture', ROOT / 'hifiti_api.py')
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: module}), mock.patch.object(socket, 'getaddrinfo') as dns:
            spec.loader.exec_module(module)
        dns.assert_not_called()

    def test_fixed_paths_reject_arbitrary_hosts_and_transactions(self):
        for method, target in [('POST', '/thread-1.htm'), ('GET', 'https://evil.example/'),
                               ('GET', '//evil.example/'), ('GET', '/user-create.htm?step=2'),
                               ('POST', '/user-send_code-user_create.htm'), ('POST', '/user-create.htm/'),
                               ('GET', '/thread-1.htm?buy=1'), ('GET', '/search.htm?keyword=x\r\nCookie: secret')]:
            with mock.patch.object(socket, 'getaddrinfo') as dns, self.assertRaises(api.ApiError):
                api._wire(method, target, api._Session())
            dns.assert_not_called()
        self.assertTrue(api._allowed_target('GET', '/search-A_2DB-0-1-2009.htm'))

    def test_dns_nonpublic_is_rejected_before_connect(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.1.1', '::1', '::ffff:127.0.0.1'):
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
            with mock.patch.object(socket, 'getaddrinfo', return_value=rows), mock.patch.object(api, '_HTTPS') as conn:
                with self.assertRaises(api.ApiError):
                    api._wire('GET', '/user-login.htm', api._Session())
                conn.assert_not_called()

    def test_global_debug_is_disabled_on_private_connection(self):
        with mock.patch.object(http.client.HTTPConnection, 'debuglevel', 1):
            conn = api._HTTPS([], time.monotonic() + 2)
            self.assertEqual(0, conn.debuglevel)
            conn.close()

    def test_transport_body_status_redirect_bounds_and_redaction(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        for status, length, chunks, code in [(302, '0', [b''], 'UPSTREAM_ERROR'),
                (403, '0', [b''], 'CHALLENGE'), (200, str(api.MAX_BODY + 1), [], 'UPSTREAM_ERROR'),
                (200, None, [b'x' * (api.MAX_BODY + 1)], 'UPSTREAM_ERROR'),
                (200, '3', [b'x', b''], 'UPSTREAM_ERROR')]:
            response = mock.Mock(status=status)
            response.getheader.side_effect = lambda name, default=None: length if name == 'Content-Length' else default
            response.read1.side_effect = chunks
            connection = mock.Mock(sock=None)
            connection.getresponse.return_value = response
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                    mock.patch.object(socket, 'getaddrinfo', return_value=public), \
                    mock.patch.object(api, '_HTTPS', return_value=connection), self.assertRaises(api.ApiError) as ctx:
                api._wire('POST', '/user-login.htm', api._Session(), form={'email': 'SECRET', 'password': 'HASHSECRET'})
            self.assertEqual(code, ctx.exception.code)
            self.assertNotIn('SECRET', str(ctx.exception) + out.getvalue())
            self.assertEqual(1, connection.request.call_count)
            connection.close.assert_called()

    def test_cookie_jar_respects_domain_and_path_and_isolated_clone(self):
        request = api.Request(api.ORIGIN + '/user-login.htm')
        session = api._Session()
        session.cookies.extract_cookies(api._CookieResponse([
            ('Set-Cookie', 'bbs_token=SECRET; Path=/; Secure; HttpOnly'),
            ('Set-Cookie', 'bad=SECRET; Domain=evil.example; Path=/')]), request)
        cloned = session.clone()
        target = api.Request(api.ORIGIN + '/thread-1.htm')
        cloned.cookies.add_cookie_header(target)
        self.assertEqual('bbs_token=SECRET', target.get_header('Cookie'))
        cloned.cookies.clear()
        self.assertEqual(1, len(session.cookies))
        self.assertEqual(0, len(api._Session().cookies))


if __name__ == '__main__':
    unittest.main()
