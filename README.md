# AI-бот для Telegram-канала

Читает RSS, делает краткие посты через Claude и публикует в канал. Работает на GitHub Actions.

## Запуск
1. Создайте бота у @BotFather, получите токен.
2. Добавьте бота в канал **администратором** с правом публикации.
3. Создайте репозиторий на GitHub и загрузите все файлы (включая папку `.github`).
4. Settings → Secrets and variables → Actions → New repository secret:
   - `TELEGRAM_BOT_TOKEN` — токен от BotFather
   - `TELEGRAM_CHANNEL_ID` — `@имя_канала` или числовой id (`-100...`)
   - `ANTHROPIC_API_KEY` — ключ с console.anthropic.com
5. Вкладка Actions → Post to Telegram → Run workflow для проверки.

## Настройка
- Источники: `feeds.txt`
- Частота: `cron` в `.github/workflows/post.yml`
- Постов за запуск, язык, модель: переменные `MAX_POSTS_PER_RUN`, `POST_LANGUAGE`, `CLAUDE_MODEL`
