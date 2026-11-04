import html
import json
import os
from pathlib import Path

import feedparser
import requests
from anthropic import Anthropic

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHANNEL = os.environ["TELEGRAM_CHANNEL_ID"]  # @channelname или -100...
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_POSTS = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
LANGUAGE = os.getenv("POST_LANGUAGE", "русском")
STATE_FILE = Path("posted.json")
FEEDS_FILE = Path("feeds.txt")

client = Anthropic()  # берёт ANTHROPIC_API_KEY из окружения


def load_state() -> list:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return []


def save_state(ids: list) -> None:
    STATE_FILE.write_text(
        json.dumps(ids[-500:], ensure_ascii=False, indent=1), encoding="utf-8"
    )


def fetch_new_entries(seen: set) -> list:
    entries = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        feed = feedparser.parse(url)
        for e in feed.entries[:10]:
            uid = e.get("id") or e.get("link")
            if uid and uid not in seen:
                entries.append(
                    {
                        "id": uid,
                        "title": e.get("title", ""),
                        "link": e.get("link", ""),
                        "summary": e.get("summary", "")[:3000],
                    }
                )
    return entries


def summarize(entry: dict) -> tuple[str, str]:
    prompt = (
        f"Ты редактор Telegram-канала. Напиши пост на {LANGUAGE} языке по новости ниже.\n"
        "Формат ответа строго: первая строка — короткий цепляющий заголовок, "
        "затем пустая строка, затем 2–4 предложения сути. Без markdown, без эмодзи-спама, "
        "ничего не выдумывай сверх текста.\n\n"
        f"Заголовок: {entry['title']}\nТекст: {entry['summary']}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    title, _, body = text.partition("\n")
    return title.strip(), body.strip()


def post_to_telegram(title: str, body: str, link: str) -> None:
    message = (
        f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}\n\n"
        f'<a href="{html.escape(link, quote=True)}">Источник</a>'
    )
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHANNEL,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    r.raise_for_status()


def main() -> None:
    posted = load_state()
    entries = fetch_new_entries(set(posted))
    print(f"Новых материалов: {len(entries)}")
    for entry in entries[:MAX_POSTS]:
        try:
            title, body = summarize(entry)
            post_to_telegram(title, body, entry["link"])
            print("Опубликовано:", title)
        except Exception as exc:  # не роняем весь запуск из-за одной новости
            print("Ошибка:", entry["link"], exc)
            continue
        posted.append(entry["id"])
    save_state(posted)


if __name__ == "__main__":
    main()
