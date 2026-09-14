import io
import json
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import main


RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Ship stdlib HTTP</title>
      <link>https://example.com/rss-item</link>
      <description>Use &lt;code&gt;urllib&lt;/code&gt; instead of extra deps.</description>
    </item>
  </channel>
</rss>
"""

ATOM_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom entry</title>
    <link href="https://example.com/atom-item"/>
    <summary>Takeaways for &lt;em&gt;Python&lt;/em&gt; readers.</summary>
  </entry>
</feed>
"""

ATOM_MIXED_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Missing link</title>
    <summary>Should be skipped, not fail the feed.</summary>
  </entry>
  <entry>
    <title>Kept entry</title>
    <link href="https://example.com/kept"/>
    <summary>ok</summary>
  </entry>
</feed>
"""


def http_error(url, code, reason, location=None):
    headers = EmailMessage()
    if location is not None:
        headers['Location'] = location
    return HTTPError(url, code, reason, headers, io.BytesIO(b''))


def mock_response(body=RSS_XML):
    response = MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class ParseRssTests(unittest.TestCase):
    def test_parses_rss_and_strips_html(self):
        entries = main.parse_rss(RSS_XML)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['title'], 'Ship stdlib HTTP')
        self.assertEqual(entries[0]['link'], 'https://example.com/rss-item')
        self.assertEqual(entries[0]['summary'], 'Use urllib instead of extra deps.')

    def test_parses_atom_link_href(self):
        entries = main.parse_rss(ATOM_XML)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['title'], 'Atom entry')
        self.assertEqual(entries[0]['link'], 'https://example.com/atom-item')
        self.assertEqual(entries[0]['summary'], 'Takeaways for Python readers.')

    def test_skips_atom_entry_without_link_and_keeps_the_rest(self):
        entries = main.parse_rss(ATOM_MIXED_XML)
        self.assertEqual([entry['link'] for entry in entries], ['https://example.com/kept'])

    def test_invalid_xml_returns_empty_list(self):
        self.assertEqual(main.parse_rss(b'not xml'), [])


class FormatTelegramMessageTests(unittest.TestCase):
    def test_escapes_html_converts_markdown_and_bullets(self):
        message = main.format_telegram_message(
            'Python',
            'Title <script> & more',
            '**Ship it**\n- first\n* second\nA <tag> & ampersand',
            'https://example.com/item',
        )
        self.assertIn('<b>[Python] Title &lt;script&gt; &amp; more</b>', message)
        self.assertIn('<b>Ship it</b>', message)
        self.assertIn('• first', message)
        self.assertIn('• second', message)
        self.assertIn('A &lt;tag&gt; &amp; ampersand', message)
        self.assertIn('<a href="https://example.com/item">Read more</a>', message)
        self.assertNotIn('<script>', message)

    def test_escapes_quotes_in_href(self):
        message = main.format_telegram_message(
            'AI',
            'Title',
            'Body',
            'https://example.com/a?x="b"&c=d',
        )
        self.assertIn('href="https://example.com/a?x=&quot;b&quot;&amp;c=d"', message)

    def test_truncates_to_telegram_limit(self):
        message = main.format_telegram_message(
            'Python',
            'Title',
            'x' * 8000,
            'https://example.com/item',
        )
        self.assertLessEqual(len(message), main.TELEGRAM_MAX_LENGTH)
        self.assertIn('…', message)
        self.assertIn('<b>[Python] Title</b>', message)
        self.assertIn('<a href="https://example.com/item">Read more</a>', message)


class PromptTests(unittest.TestCase):
    def test_summary_length_is_wired_into_the_prompt(self):
        prompt = main.build_summary_prompt('Python', 'Title', 'Body', 'short')
        self.assertIn(main.SUMMARY_LENGTH_HINTS['short'], prompt)
        self.assertIn('Python', prompt)
        self.assertIn('Title', prompt)
        self.assertNotIn(main.SUMMARY_LENGTH_HINTS['long'], prompt)

    def test_unknown_summary_length_falls_back_to_medium(self):
        prompt = main.build_summary_prompt('AI', 'T', 'C', 'not-a-size')
        self.assertIn(main.SUMMARY_LENGTH_HINTS['medium'], prompt)


class ConfigTests(unittest.TestCase):
    def test_repo_config_has_required_source_fields(self):
        config = main.load_config()
        self.assertTrue(config['sources'])
        for source in config['sources']:
            self.assertTrue(source['name'])
            self.assertTrue(source['url'].startswith('https://'))
            self.assertTrue(source['niche'])
        self.assertIsInstance(config['settings']['max_entries_per_run'], int)
        self.assertIn(config['settings']['summary_length'], main.SUMMARY_LENGTH_HINTS)


class EnvTests(unittest.TestCase):
    def test_load_env_sets_values_from_file(self):
        previous = os.environ.pop('TELEGRAM_CHAT_ID', None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_path = Path(tmp) / '.env'
                env_path.write_text('TELEGRAM_CHAT_ID=12345\n# comment\n', encoding='utf-8')
                main.load_env(env_path)
                self.assertEqual(os.environ['TELEGRAM_CHAT_ID'], '12345')
        finally:
            if previous is None:
                os.environ.pop('TELEGRAM_CHAT_ID', None)
            else:
                os.environ['TELEGRAM_CHAT_ID'] = previous


class PathTests(unittest.TestCase):
    def test_load_config_uses_script_dir_not_cwd(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                config = main.load_config()
            finally:
                os.chdir(cwd)
        self.assertTrue(config['sources'])


class ProcessedStoreTests(unittest.TestCase):
    def test_corrupt_processed_file_returns_empty_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'processed_entries.json')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('{not-json')
            self.assertEqual(main.load_processed(path), set())


class FetchRssTests(unittest.TestCase):
    def test_returns_none_on_network_error(self):
        with patch('urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertIsNone(main.fetch_rss('https://example.com/feed'))

    def test_passes_http_timeout(self):
        with patch('urllib.request.urlopen', return_value=mock_response()) as urlopen:
            main.fetch_rss('https://example.com/feed')
        self.assertEqual(urlopen.call_args.kwargs['timeout'], main.HTTP_TIMEOUT)

    def test_follows_relative_308_redirect(self):
        redirect = http_error('https://example.com/feed', 308, 'Permanent Redirect', '/rss/')
        try:
            with patch(
                'urllib.request.urlopen', side_effect=[redirect, mock_response()]
            ) as urlopen:
                body = main.fetch_rss('https://example.com/feed')
        finally:
            redirect.close()

        self.assertEqual(body, RSS_XML)
        second_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(second_request.full_url, 'https://example.com/rss/')

    def test_308_hop_limit_returns_none(self):
        errors = [
            http_error('https://example.com/feed', 308, 'Permanent Redirect', '/rss/')
            for _ in range(main.MAX_REDIRECTS + 2)
        ]
        try:
            with patch('urllib.request.urlopen', side_effect=errors) as urlopen:
                body = main.fetch_rss('https://example.com/feed')
        finally:
            for error in errors:
                error.close()

        self.assertIsNone(body)
        self.assertEqual(urlopen.call_count, main.MAX_REDIRECTS + 1)

    def test_non_308_http_error_returns_none(self):
        error = http_error('https://example.com/feed', 404, 'Not Found')
        try:
            with patch('urllib.request.urlopen', side_effect=error) as urlopen:
                body = main.fetch_rss('https://example.com/feed')
        finally:
            error.close()
        self.assertIsNone(body)
        self.assertEqual(urlopen.call_count, 1)


class GeminiTests(unittest.TestCase):
    def test_retries_on_429_then_returns_text(self):
        limited = http_error(
            'https://generativelanguage.googleapis.com', 429, 'Too Many Requests'
        )
        payload = json.dumps({
            'candidates': [{'content': {'parts': [{'text': 'takeaway'}]}}]
        }).encode('utf-8')
        try:
            with patch('main.time.sleep') as sleep:
                with patch(
                    'urllib.request.urlopen', side_effect=[limited, mock_response(payload)]
                ) as urlopen:
                    text = main.call_gemini('fake-key', 'summarize this')
        finally:
            limited.close()

        self.assertEqual(text, 'takeaway')
        sleep.assert_called_once()
        request = urlopen.call_args_list[0].args[0]
        self.assertNotIn('key=', request.full_url)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get('x-goog-api-key'), 'fake-key')
        self.assertEqual(urlopen.call_args.kwargs['timeout'], main.HTTP_TIMEOUT)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base_dir_patcher = patch.object(main, 'BASE_DIR', Path(self.tmp.name))
        self.base_dir_patcher.start()
        self.addCleanup(self.base_dir_patcher.stop)
        self.env_backup = {
            key: os.environ.pop(key, None) for key in main.REQUIRED_ENV
        }

    def tearDown(self):
        for key, value in self.env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_config(self, max_entries=1, summary_length='medium'):
        with open(main.BASE_DIR / 'config.json', 'w', encoding='utf-8') as handle:
            json.dump({
                'sources': [{
                    'name': 'Test Weekly',
                    'url': 'https://example.com/rss/',
                    'niche': 'Python',
                }],
                'settings': {
                    'max_entries_per_run': max_entries,
                    'summary_length': summary_length,
                },
            }, handle)

    def set_secrets(self):
        os.environ['TELEGRAM_BOT_TOKEN'] = 'token'
        os.environ['TELEGRAM_CHAT_ID'] = 'chat'
        os.environ['GEMINI_API_KEY'] = 'gemini'

    def processed_path(self):
        return main.BASE_DIR / 'processed_entries.json'

    def test_missing_keys_without_dry_run_exits_nonzero(self):
        code = main.main([])
        self.assertEqual(code, 1)
        self.assertFalse(self.processed_path().exists())

    def test_dry_run_does_not_persist_sleep_or_call_side_effects(self):
        self.write_config()
        with patch('main.fetch_rss', return_value=RSS_XML), \
             patch('main.call_gemini') as gemini, \
             patch('main.send_telegram') as telegram, \
             patch('main.time.sleep') as sleep:
            code = main.main(['--dry-run'])
        self.assertEqual(code, 0)
        self.assertFalse(self.processed_path().exists())
        gemini.assert_not_called()
        telegram.assert_not_called()
        sleep.assert_not_called()

    def test_failed_send_does_not_persist(self):
        self.write_config()
        self.set_secrets()
        with patch('main.fetch_rss', return_value=RSS_XML), \
             patch('main.call_gemini', return_value='takeaway'), \
             patch('main.send_telegram', return_value=None), \
             patch('main.time.sleep'):
            code = main.main([])
        self.assertEqual(code, 0)
        self.assertFalse(self.processed_path().exists())

    def test_successful_send_persists_immediately(self):
        self.write_config()
        self.set_secrets()
        with patch('main.fetch_rss', return_value=RSS_XML), \
             patch('main.call_gemini', return_value='takeaway') as gemini, \
             patch('main.send_telegram', return_value={'ok': True}), \
             patch('main.time.sleep') as sleep:
            code = main.main([])
        self.assertEqual(code, 0)
        stored = main.load_processed()
        self.assertEqual(stored, {'https://example.com/rss-item'})
        prompt = gemini.call_args.args[1]
        self.assertIn(main.SUMMARY_LENGTH_HINTS['medium'], prompt)
        sleep.assert_called_once_with(main.ITEM_PAUSE_SECONDS)


if __name__ == '__main__':
    unittest.main()
