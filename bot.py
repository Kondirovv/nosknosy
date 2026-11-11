import html
import json
import os
import re
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
        raw = STATE_FILE.read_text(encoding="utf-8").strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                print("posted.json повреждён, начинаю с пустого списка")
    return []


def save_state(ids: list) -> None:
    STATE_FILE.write_text(
        json.dumps(ids[-500:], ensure_ascii=False, indent=1), encoding="utf-8"
    )


def clean_html(text: str) -> str:
    """Убирает HTML-теги из описания новости."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_new_entries(seen: set) -> list:
    entries = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            print("Не удалось прочитать ленту:", url, exc)
            continue
        for e in feed.entries[:10]:
            uid = e.get("id") or e.get("link")
            if uid and uid not in seen:
                entries.append(
                    {
                        "id": uid,
                        "title": clean_html(e.get("title", "")),
                        "link": e.get("link", ""),
                        "summary": clean_html(e.get("summary", ""))[:3000],
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
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise ValueError("Claude вернул пустой ответ")
    title, _, body = text.partition("\n")
    return title.strip(), body.strip()


def post_to_telegram(title: str, body: str, link: str) -> None:
    message = f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}"
    if link:
        message += f'\n\n<a href="{html.escape(link, quote=True)}">Источник</a>'
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHANNEL,
            "text": message[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not r.ok:
        # показываем причину от Telegram, а не только код ошибки
        raise RuntimeError(f"Telegram {r.status_code}: {r.text}")


def main() -> None:
    posted = load_state()
    entries = fetch_new_entries(set(posted))
    print(f"Новых материалов: {len(entries)}")
    published = 0
    for entry in entries:
        if published >= MAX_POSTS:
            break
        try:
            title, body = summarize(entry)
            post_to_telegram(title, body, entry["link"])
            print("Опубликовано:", title)
        except Exception as exc:  # не роняем весь запуск из-за одной новости
            print("Ошибка:", entry["link"], exc)
            continue
        posted.append(entry["id"])
        published += 1
        save_state(posted)  # сохраняем сразу, чтобы не было дублей при сбое
    save_state(posted)


if __name__ == "__main__":
    main()
