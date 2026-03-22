# Newsletter Triage System

An automated, dependency-free Python script to scrape newsletters (via RSS), summarize them using AI, and deliver the results to Telegram.

## Setup

1. **Configure Sources**: Edit `config.json` to add your favorite newsletter RSS feeds and define their niche categories.
2. **Environment Variables**:
   - Copy `.env.example` to `.env`.
   - Add your `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   - Add your `GEMINI_API_KEY` for AI summarization.
3. **Run a Dry Run**: Run `python3 main.py` without a `.env` file to see if the scraping works as expected.

## Automation (Cron)

To run this automatically every hour, add it to your crontab:

1. Open crontab:
   ```bash
   crontab -e
   ```
2. Add the following line (adjust paths):
   ```bash
   0 * * * * cd /Users/c/.gemini/antigravity/scratch/newsletter-triage && /usr/bin/python3 main.py >> triage.log 2>&1
   ```
