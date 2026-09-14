import io
import json
import os
import tempfile
import unittest
from email.message import EmailMessage
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
        self.assertIn("<a href='https://example.com/item'>Read more</a>", message)
        self.assertNotIn('<script>', message)


class ConfigTests(unittest.TestCase):
    def test_repo_config_has_required_source_fields(self):
        config = main.load_config()
        self.assertTrue(config['sources'])
        for source in config['sources']:
            self.assertTrue(source['name'])
            self.assertTrue(source['url'].startswith('https://'))
            self.assertTrue(source['niche'])
        self.assertIsInstance(config['settings']['max_entries_per_run'], int)


class EnvTests(unittest.TestCase):
    def test_load_env_sets_values_from_file(self):
        previous = os.environ.pop('TELEGRAM_CHAT_ID', None)
        cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                with open('.env', 'w', encoding='utf-8') as handle:
                    handle.write('TELEGRAM_CHAT_ID=12345\n# comment\n')
                main.load_env()
                self.assertEqual(os.environ['TELEGRAM_CHAT_ID'], '12345')
        finally:
            os.chdir(cwd)
            if previous is None:
                os.environ.pop('TELEGRAM_CHAT_ID', None)
            else:
                os.environ['TELEGRAM_CHAT_ID'] = previous


class FetchRssTests(unittest.TestCase):
    def test_returns_none_on_network_error(self):
        with patch('urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertIsNone(main.fetch_rss('https://example.com/feed'))

    def test_follows_relative_308_redirect(self):
        headers = EmailMessage()
        headers['Location'] = '/rss/'
        redirect = HTTPError(
            'https://example.com/feed',
            308,
            'Permanent Redirect',
            headers,
            io.BytesIO(b''),
        )
        response = MagicMock()
        response.read.return_value = RSS_XML
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        try:
            with patch('urllib.request.urlopen', side_effect=[redirect, response]) as urlopen:
                body = main.fetch_rss('https://example.com/feed')
        finally:
            redirect.close()

        self.assertEqual(body, RSS_XML)
        second_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(second_request.full_url, 'https://example.com/rss/')


class GeminiTests(unittest.TestCase):
    def test_retries_on_429_then_returns_text(self):
        limited = HTTPError(
            'https://generativelanguage.googleapis.com',
            429,
            'Too Many Requests',
            EmailMessage(),
            io.BytesIO(b''),
        )
        response = MagicMock()
        response.read.return_value = json.dumps({
            'candidates': [{'content': {'parts': [{'text': 'takeaway'}]}}]
        }).encode('utf-8')
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        try:
            with patch('main.time.sleep') as sleep:
                with patch('urllib.request.urlopen', side_effect=[limited, response]):
                    text = main.call_gemini('fake-key', 'summarize this')
        finally:
            limited.close()

        self.assertEqual(text, 'takeaway')
        sleep.assert_called_once()


class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_persist_or_call_side_effects(self):
        cwd = os.getcwd()
        env_backup = {
            key: os.environ.pop(key, None)
            for key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'GEMINI_API_KEY')
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                with open('config.json', 'w', encoding='utf-8') as handle:
                    json.dump({
                        'sources': [{
                            'name': 'Test Weekly',
                            'url': 'https://example.com/rss/',
                            'niche': 'Python',
                        }],
                        'settings': {'max_entries_per_run': 1},
                    }, handle)
                with patch('main.fetch_rss', return_value=RSS_XML), \
                     patch('main.call_gemini') as gemini, \
                     patch('main.send_telegram') as telegram, \
                     patch('main.time.sleep'):
                    main.main()
                self.assertFalse(os.path.exists('processed_entries.json'))
                gemini.assert_not_called()
                telegram.assert_not_called()
        finally:
            os.chdir(cwd)
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == '__main__':
    unittest.main()
