import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HTTP_TIMEOUT = 20
MAX_REDIRECTS = 5
PROCESSED_FILE = 'processed_entries.json'
ATOM_NS = '{http://www.w3.org/2005/Atom}'
REQUIRED_ENV = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'GEMINI_API_KEY')
SUMMARY_LENGTH_HINTS = {
    'short': 'Keep it to 2-3 short bullet points.',
    'medium': 'Use 4-6 bullet points.',
    'long': 'Use up to 10 bullet points with brief context for each.',
}


def load_config():
    with open('config.json', 'r') as f:
        return json.load(f)


def load_env():
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                if '=' in line:
                    key, value = line.strip().split('=', 1)
                    os.environ[key] = value


def load_processed(path=PROCESSED_FILE):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return set(data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return set()


def save_processed(processed, path=PROCESSED_FILE):
    with open(path, 'w') as f:
        json.dump(sorted(processed), f)


def fetch_rss(url, redirects_left=MAX_REDIRECTS):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            try:
                if e.code == 308 and redirects_left > 0:
                    new_url = e.headers.get('Location')
                    if new_url:
                        if new_url.startswith('/'):
                            parsed = urllib.parse.urlparse(url)
                            new_url = f"{parsed.scheme}://{parsed.netloc}{new_url}"
                        print(f"[*] Following 308 redirect to: {new_url}")
                        return fetch_rss(new_url, redirects_left - 1)
                print(f"Error fetching {url}: {e}")
                return None
            finally:
                e.close()
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None


def entry_link(item):
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


def parse_rss(xml_data):
    entries = []
    try:
        root = ET.fromstring(xml_data)
        items = root.findall('.//item') or root.findall(f'.//{ATOM_NS}entry')
        for item in items:
            try:
                title = item.findtext('title') or item.findtext(f'{ATOM_NS}title')
                link = entry_link(item)
                if not title or not link:
                    continue
                desc = (
                    item.findtext('description')
                    or item.findtext(f'{ATOM_NS}summary')
                    or item.findtext(f'{ATOM_NS}content')
                    or ''
                )
                desc = re.sub('<[^<]+?>', '', desc)
                entries.append({'title': title, 'link': link, 'summary': desc})
            except Exception as e:
                print(f"Error parsing entry: {e}")
                continue
    except Exception as e:
        print(f"Error parsing XML: {e}")
    return entries


def build_summary_prompt(niche, title, content, summary_length='medium'):
    length_hint = SUMMARY_LENGTH_HINTS.get(summary_length, SUMMARY_LENGTH_HINTS['medium'])
    return (
        f"Summarize the following newsletter entry for a {niche} niche. "
        f"Focus on practical takeaways and high-signal news. {length_hint} "
        f"Title: {title}. Content snippet: {content[:1000]}"
    )


def call_gemini(api_key, prompt, retries=3):
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
                if e.code == 429:
                    wait_time = (2 ** i) + 5
                    print(f"[!] Rate limited (429). Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                print(f"Error calling Gemini: {e}")
                break
            finally:
                e.close()
        except Exception as e:
            print(f"Error calling Gemini: {e}")
            break
    return None


def format_telegram_message(niche, title, summary, link):
    html_summary = summary.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    html_summary = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', html_summary)
    html_summary = re.sub(r'^\s*[\*\-]\s+', '• ', html_summary, flags=re.MULTILINE)
    safe_title = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return f"<b>[{niche}] {safe_title}</b>\n\n{html_summary}\n\n<a href='{link}'>Read more</a>"


def send_telegram(token, chat_id, message):
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
        print(f"Error sending to Telegram: {e} - {e.read().decode('utf-8')}")
        return None
    except Exception as e:
        print(f"Error sending to Telegram: {e}")
        return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Summarize newsletter feeds and send briefs to Telegram.')
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Fetch and format without calling Gemini or Telegram, and without saving processed IDs.',
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    load_env()

    secrets = {key: os.getenv(key) for key in REQUIRED_ENV}
    missing = [key for key, value in secrets.items() if not value]
    if args.dry_run:
        dry_run = True
        print('--- DRY RUN MODE ---')
        print('Scraping will proceed, but summaries will be placeholders and nothing will be sent to Telegram.\n')
    elif missing:
        print('Missing required environment variables: ' + ', '.join(missing))
        print('Set them in .env, or pass --dry-run to scrape without sending.')
        return 1
    else:
        dry_run = False

    config = load_config()

    processed = load_processed()
    sent_this_run = False

    for source in config['sources']:
        print(f"[*] Processing {source['name']} ({source['url']})...")
        xml_data = fetch_rss(source['url'])
        if not xml_data:
            print(f"[!] Skipping {source['name']} due to fetch error.")
            continue

        entries = parse_rss(xml_data)
        print(f"[+] Found {len(entries)} entries.")
        count = 0
        max_entries = config['settings'].get('max_entries_per_run', 5)
        summary_length = config['settings'].get('summary_length', 'medium')
        for entry in entries:
            if entry['link'] in processed:
                continue
            if count >= max_entries:
                print(f"[-] Reached limit for {source['name']}.")
                break

            print(f"[-] Processing: {entry['title'][:50]}...")

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

            if not summary:
                continue

            formatted_msg = format_telegram_message(
                source['niche'], entry['title'], summary, entry['link']
            )

            if dry_run:
                print(f"\nFORMATED MESSAGE:\n{formatted_msg}\n")
                count += 1
                time.sleep(5)
                continue

            result = send_telegram(
                secrets['TELEGRAM_BOT_TOKEN'], secrets['TELEGRAM_CHAT_ID'], formatted_msg
            )
            if not result:
                print(f"[!] Telegram send failed for {entry['link']}; leaving unprocessed.")
                count += 1
                time.sleep(5)
                continue

            processed.add(entry['link'])
            save_processed(processed)
            sent_this_run = True
            count += 1
            time.sleep(5)

    if dry_run:
        print('\n[DONE] Dry run complete. No entries were saved as processed.')
    elif sent_this_run:
        print('\n[DONE] Processed entries saved.')
    else:
        print('\n[DONE] No new entries sent.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
