"""
Sends a Telegram message when a newly scraped job scores above a
match threshold. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in
.env - get these by messaging @BotFather on Telegram to create a bot,
and @userinfobot to find your own chat ID.

NOTE: this sandbox can't reach api.telegram.org, so this has only been
tested for correct request construction (see the mocked test), not a
real message delivery. Test this for real on your own machine once
you have a bot token.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_alert(job, site_url: str = "http://127.0.0.1:8000") -> tuple[bool, str]:
    """
    Returns (success, message) rather than raising - a failed Telegram
    send should never take down the scraper run that triggered it.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return False, "Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)"

    text = (
        f"🎯 New high-match job!\n\n"
        f"{job.title} at {job.company}\n"
        f"Match: {job.match_score}%\n"
        f"{site_url}/matches/{job.id}/"
    )

    try:
        response = requests.post(
            TELEGRAM_API_URL.format(token=token),
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if response.ok:
            return True, "Sent"
        return False, f"Telegram API error: {response.status_code} {response.text}"
    except requests.RequestException as e:
        return False, f"Network error sending Telegram alert: {e}"
