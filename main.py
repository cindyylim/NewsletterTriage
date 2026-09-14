from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

HTTP_TIMEOUT = 20
MAX_REDIRECTS = 5
ITEM_PAUSE_SECONDS = 5
TELEGRAM_MAX_LENGTH = 4096
USER_AGENT = 'NewsletterTriage/1.0'
BASE_DIR = Path(__file__).resolve().parent
ATOM_NS = '{http://www.w3.org/2005/Atom}'
REQUIRED_ENV = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'GEMINI_API_KEY')
GEMINI_RETRY_STATUS = {429, 500, 503}
HTML_TAG_RE = re.compile(r'<[^>]+>')
SUMMARY_LENGTH_HINTS = {
    'short': 'Keep it to 2-3 short bullet points.',
    'medium': 'Use 4-6 bullet points.',
    'long': 'Use up to 10 bullet points with brief context for each.',
}

logger = logging.getLogger('triage')


def setup_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )


def _data_path(path: Path | str | None, name: str) -> Path:
    return Path(path) if path is not None else BASE_DIR / name


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    with open(_data_path(path, 'config.json'), 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_config(config: Any) -> list[str]:
    if not isinstance(config, dict):
        return ['config.json must be an object']
    errors: list[str] = []
    sources = config.get('sources')
    if not isinstance(sources, list) or not sources:
        errors.append('config.json must have a non-empty sources array')
    else:
        for i, source in enumerate(sources):
            if not isinstance(source, dict):
                errors.append(f'sources[{i}] must be an object')
                continue
            for field in ('name', 'url', 'niche'):
                if not source.get(field):
                    errors.append(f'sources[{i}] missing {field}')
            url = source.get('url', '')
            if url and not str(url).startswith(('https://', 'http://')):
                errors.append(f'sources[{i}] url must be http(s)')
    settings = config.get('settings', {})
    if settings is not None and not isinstance(settings, dict):
        errors.append('settings must be an object')
    return errors


def load_env(path: Path | str | None = None) -> None:
    env_path = _data_path(path, '.env')
    if not env_path.exists():
        return
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            if stripped.startswith('export '):
                stripped = stripped[len('export '):].lstrip()
            key, value = stripped.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or key in os.environ:
                continue
            os.environ[key] = value


def load_processed(path: Path | str | None = None) -> set[str]:
    processed_path = _data_path(path, 'processed_entries.json')
    if not processed_path.exists():
        return set()
    try:
        with open(processed_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {str(item) for item in data}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return set()


def save_processed(processed: set[str], path: Path | str | None = None) -> None:
    processed_path = _data_path(path, 'processed_entries.json')
    tmp_path = processed_path.with_name(processed_path.name + '.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(sorted(processed), f)
    tmp_path.replace(processed_path)


def fetch_rss(url: str, redirects_left: int = MAX_REDIRECTS) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            try:
                if e.code == 308 and redirects_left > 0:
                    location = e.headers.get('Location')
                    if location:
                        new_url = urllib.parse.urljoin(url, location)
                        logger.info('Following 308 redirect to: %s', new_url)
                        return fetch_rss(new_url, redirects_left - 1)
                logger.error('Error fetching %s: %s', url, e)
                return None
            finally:
                e.close()
    except Exception as e:
        logger.error('Error fetching %s: %s', url, e)
        return None


def entry_link(item: ET.Element) -> str | None:
    text = item.findtext('link')
    if text and text.strip():
        return text.strip()
    link_el = item.find('link') or item.find(f'{ATOM_NS}link')
    if link_el is None:
        return None
    href = link_el.get('href')
    if href and href.strip():
        return href.strip()
    if link_el.text and link_el.text.strip():
        return link_el.text.strip()
    return None


def _child_text(item: ET.Element, *names: str) -> str:
    for name in names:
        el = item.find(name)
        if el is None:
            continue
        raw = html.unescape(''.join(el.itertext()))
        text = HTML_TAG_RE.sub('', raw).strip()
        if text:
            return text
    return ''


def parse_rss(xml_data: bytes | str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_data)
        items = root.findall('.//item') or root.findall(f'.//{ATOM_NS}entry')
        for item in items:
            try:
                title = _child_text(item, 'title', f'{ATOM_NS}title')
                link = entry_link(item)
                if not title or not link:
                    continue
                desc = _child_text(
                    item, 'description', f'{ATOM_NS}summary', f'{ATOM_NS}content'
                )
                entries.append({'title': title, 'link': link, 'summary': desc})
            except Exception as e:
                logger.error('Error parsing entry: %s', e)
                continue
    except Exception as e:
        logger.error('Error parsing XML: %s', e)
    return entries


def build_summary_prompt(
    niche: str, title: str, content: str, summary_length: str = 'medium'
) -> str:
    length_hint = SUMMARY_LENGTH_HINTS.get(summary_length, SUMMARY_LENGTH_HINTS['medium'])
    return (
        f"Summarize the following newsletter entry for a {niche} niche. "
        f"Focus on practical takeaways and high-signal news. {length_hint} "
        f"Title: {title}. Content snippet: {content[:1000]}"
    )


def call_gemini(api_key: str, prompt: str, retries: int = 3) -> str | None:
    url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent'
    data = {'contents': [{'parts': [{'text': prompt}]}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': api_key,
        },
    )

    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result['candidates'][0]['content']['parts'][0]['text']
        except urllib.error.HTTPError as e:
            try:
                if e.code in GEMINI_RETRY_STATUS and i < retries - 1:
                    wait_time = (2 ** i) + 5
                    logger.warning(
                        'Gemini HTTP %s. Retrying in %ss...', e.code, wait_time
                    )
                    time.sleep(wait_time)
                    continue
                logger.error('Error calling Gemini: %s', e)
                break
            finally:
                e.close()
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            if i < retries - 1:
                wait_time = (2 ** i) + 5
                logger.warning('Gemini request failed (%s). Retrying in %ss...', e, wait_time)
                time.sleep(wait_time)
                continue
            logger.error('Error calling Gemini: %s', e)
            break
        except Exception as e:
            logger.error('Error calling Gemini: %s', e)
            break
    return None


def _fit_telegram_text(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ''
    if len(text) <= max_len:
        return text
    if max_len == 1:
        return '…'
    cut = text[: max_len - 1]
    lt = cut.rfind('<')
    gt = cut.rfind('>')
    if lt > gt:
        cut = cut[:lt]
    return cut.rstrip() + '…'


def format_telegram_message(niche: str, title: str, summary: str, link: str) -> str:
    header = f"<b>[{html.escape(niche)}] {html.escape(title)}</b>\n\n"
    footer = f"\n\n<a href=\"{html.escape(link, quote=True)}\">Read more</a>"
    body = html.escape(summary)
    body = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', body)
    body = re.sub(r'^\s*[\*\-]\s+', '• ', body, flags=re.MULTILINE)

    budget = TELEGRAM_MAX_LENGTH - len(header) - len(footer)
    if budget < 0:
        clipped_header = _fit_telegram_text(header, TELEGRAM_MAX_LENGTH - len(footer))
        return clipped_header + footer
    return header + _fit_telegram_text(body, budget) + footer


def telegram_send_succeeded(result: Any) -> bool:
    return isinstance(result, dict) and result.get('ok') is True


def send_telegram(token: str, chat_id: str, message: str) -> dict[str, Any] | None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'false',
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        logger.error('Error sending to Telegram: %s - %s', e, e.read().decode('utf-8'))
        return None
    except Exception as e:
        logger.error('Error sending to Telegram: %s', e)
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Summarize newsletter feeds and send briefs to Telegram.')
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Fetch and format without calling Gemini or Telegram, and without saving processed IDs.',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    load_env()

    secrets = {key: os.getenv(key) for key in REQUIRED_ENV}
    missing = [key for key, value in secrets.items() if not value]
    if args.dry_run:
        dry_run = True
        logger.info('DRY RUN MODE — scrape and format only; nothing is sent or saved.')
    elif missing:
        logger.error('Missing required environment variables: %s', ', '.join(missing))
        logger.error('Set them in .env, or pass --dry-run to scrape without sending.')
        return 1
    else:
        dry_run = False

    try:
        config = load_config()
    except (OSError, json.JSONDecodeError) as e:
        logger.error('Could not load config.json: %s', e)
        return 1
    config_errors = validate_config(config)
    if config_errors:
        for error in config_errors:
            logger.error(error)
        return 1

    processed = load_processed()
    sent_this_run = False
    sources_ok = 0
    sources_failed = 0
    send_failures = 0
    live_attempts = 0

    for source in config['sources']:
        logger.info('Processing %s (%s)...', source['name'], source['url'])
        xml_data = fetch_rss(source['url'])
        if not xml_data:
            logger.warning('Skipping %s due to fetch error.', source['name'])
            sources_failed += 1
            continue
        sources_ok += 1

        entries = parse_rss(xml_data)
        logger.info('Found %s entries.', len(entries))
        count = 0
        settings = config.get('settings') or {}
        max_entries = settings.get('max_entries_per_run', 5)
        summary_length = settings.get('summary_length', 'medium')
        for entry in entries:
            if entry['link'] in processed:
                continue
            if count >= max_entries:
                logger.info('Reached limit for %s.', source['name'])
                break

            logger.info('Processing: %s...', entry['title'][:50])

            if dry_run:
                summary = (
                    f"MOCK SUMMARY: High-signal content for {source['niche']} niche. "
                    '(Dry run mode - provide GEMINI_API_KEY to see real summaries)'
                )
            else:
                prompt = build_summary_prompt(
                    source['niche'], entry['title'], entry['summary'], summary_length
                )
                summary = call_gemini(secrets['GEMINI_API_KEY'], prompt)
                live_attempts += 1

            if not summary:
                count += 1
                continue

            formatted_msg = format_telegram_message(
                source['niche'], entry['title'], summary, entry['link']
            )

            if dry_run:
                logger.info('FORMATTED MESSAGE:\n%s', formatted_msg)
                count += 1
                continue

            result = send_telegram(
                secrets['TELEGRAM_BOT_TOKEN'], secrets['TELEGRAM_CHAT_ID'], formatted_msg
            )
            if not telegram_send_succeeded(result):
                logger.warning(
                    'Telegram send failed for %s; leaving unprocessed.', entry['link']
                )
                send_failures += 1
                count += 1
                time.sleep(ITEM_PAUSE_SECONDS)
                continue

            processed.add(entry['link'])
            save_processed(processed)
            sent_this_run = True
            count += 1
            time.sleep(ITEM_PAUSE_SECONDS)

    if dry_run:
        logger.info('Dry run complete. No entries were saved as processed.')
    elif sent_this_run:
        logger.info('Processed entries saved.')
    else:
        logger.info('No new entries sent.')

    if sources_ok == 0 and sources_failed > 0:
        return 1
    if send_failures:
        return 1
    if not dry_run and live_attempts and not sent_this_run:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
