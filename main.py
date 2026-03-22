import os
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import re
import time
from datetime import datetime

# Load configuration
def load_config():
    with open('config.json', 'r') as f:
        return json.load(f)

# Simple .env loader
def load_env():
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                if '=' in line:
                    key, value = line.strip().split('=', 1)
                    os.environ[key] = value

def fetch_rss(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        # Use an OpenerDirector to handle more HTTP codes if needed, 
        # but urlopen handles basic redirects (301, 302, 303, 307). 
        # 308 is newer, so we add a simple retry if it fails specifically with 308.
        try:
            with urllib.request.urlopen(req) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 308:
                new_url = e.headers.get('Location')
                if new_url:
                    # Handle relative redirects
                    if new_url.startswith('/'):
                        parsed = urllib.parse.urlparse(url)
                        new_url = f"{parsed.scheme}://{parsed.netloc}{new_url}"
                    print(f"[*] Following 308 redirect to: {new_url}")
                    return fetch_rss(new_url)
            raise e
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def parse_rss(xml_data):
    entries = []
    try:
        root = ET.fromstring(xml_data)
        # Handle both RSS 2.0 and Atom
        items = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
        for item in items:
            title = item.findtext('title') or item.findtext('{http://www.w3.org/2005/Atom}title')
            link = item.findtext('link') or item.find('{http://www.w3.org/2005/Atom}link').get('href')
            desc = item.findtext('description') or item.findtext('{http://www.w3.org/2005/Atom}summary') or ""
            # Clean HTML from description
            desc = re.sub('<[^<]+?>', '', desc)
            entries.append({'title': title, 'link': link, 'summary': desc})
    except Exception as e:
        print(f"Error parsing XML: {e}")
    return entries

def call_gemini(api_key, prompt, retries=3):
    # Updated to v1beta and gemini-flash-latest for 2026 compatibility
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_key}"
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
    
    for i in range(retries):
        try:
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result['candidates'][0]['content']['parts'][0]['text']
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_time = (2 ** i) + 5  # Exponential backoff (5, 7, 11 seconds)
                print(f"[!] Rate limited (429). Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            print(f"Error calling Gemini: {e}")
            break
        except Exception as e:
            print(f"Error calling Gemini: {e}")
            break
    return None

def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Switch to HTML mode which is more robust for automatic content
    data = urllib.parse.urlencode({
        'chat_id': chat_id, 
        'text': message, 
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'false'
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"Error sending to Telegram: {e} - {e.read().decode('utf-8')}")
        return None
    except Exception as e:
        print(f"Error sending to Telegram: {e}")
        return None

def main():
    load_env()
    config = load_config()
    
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    gemini_key = os.getenv('GEMINI_API_KEY')
    
    dry_run = not all([bot_token, chat_id, gemini_key])
    if dry_run:
        print("--- DRY RUN MODE (Missing API Keys) ---")
        print("Scraping will proceed, but summaries will be placeholders and nothing will be sent to Telegram.\n")

    processed_file = 'processed_entries.json'
    if os.path.exists(processed_file):
        try:
            with open(processed_file, 'r') as f:
                processed = set(json.load(f))
        except:
            processed = set()
    else:
        processed = set()

    new_processed = set(processed)
    
    for source in config['sources']:
        print(f"[*] Processing {source['name']} ({source['url']})...")
        xml_data = fetch_rss(source['url'])
        if not xml_data: 
            print(f"[!] Skipping {source['name']} due to fetch error.")
            continue
        
        entries = parse_rss(xml_data)
        print(f"[+] Found {len(entries)} entries.")
        count = 0
        for entry in entries:
            if entry['link'] in processed: continue
            if count >= config['settings'].get('max_entries_per_run', 5): 
                print(f"[-] Reached limit for {source['name']}.")
                break
            
            print(f"[-] Processing: {entry['title'][:50]}...")
            
            if dry_run:
                summary = f"MOCK SUMMARY: High-signal content for {source['niche']} niche. (Dry run mode - provide GEMINI_API_KEY to see real summaries)"
            else:
                prompt = (f"Summarize the following newsletter entry for a {source['niche']} niche. "
                          f"Focus on practical takeaways and high-signal news. Use bullet points. "
                          f"Title: {entry['title']}. Content snippet: {entry['summary'][:1000]}")
                summary = call_gemini(gemini_key, prompt)
            
            if summary:
                # Basic Markdown to HTML conversion for Telegram
                html_summary = summary.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                # Convert bold **text** to <b>text</b>
                html_summary = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', html_summary)
                # Convert bullet points * or - at start of lines to •
                html_summary = re.sub(r'^\s*[\*\-]\s+', '• ', html_summary, flags=re.MULTILINE)
                
                safe_title = entry['title'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                formatted_msg = f"<b>[{source['niche']}] {safe_title}</b>\n\n{html_summary}\n\n<a href='{entry['link']}'>Read more</a>"
                
                if dry_run:
                    print(f"\nFORMATED MESSAGE:\n{formatted_msg}\n")
                else:
                    send_telegram(bot_token, chat_id, formatted_msg)
                
                new_processed.add(entry['link'])
                count += 1
                # Increased sleep to avoid rate limits (429). 
                time.sleep(5)

    if not dry_run:
        with open(processed_file, 'w') as f:
            json.dump(list(new_processed), f)
        print("\n[DONE] Processed entries saved.")
    else:
        print("\n[DONE] Dry run complete. No entries were saved as processed.")

if __name__ == "__main__":
    main()
