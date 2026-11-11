# Установка Creator Discord Bot на Bothost

## Файлы
В корне архива находятся `bot.py`, `requirements.txt`, `README.md` и папка `data`.

## Настройки запуска
- Язык: Python
- Версия: Python 3.11 (если в панели есть выбор версии)
- Главный файл: `bot.py`
- Команда запуска, если требуется: `python -u bot.py`
- Зависимости: автоматически из `requirements.txt`

В `requirements.txt` оставлена одна зависимость — `discord.py`. Библиотека `aiohttp` устанавливается как её обязательная зависимость. Загрузчик `.env` реализован стандартными средствами Python, поэтому отдельный пакет `python-dotenv` не нужен.

## Установка
1. Останови текущий контейнер.
2. Сделай резервную копию папки `data` (особенно `data/bot.sqlite3`).
3. Загрузи архив и распакуй его в корень проекта.
4. Проверь, что `bot.py` и `requirements.txt` лежат непосредственно в корне, а не во вложенной папке.
5. В переменных окружения Bothost создай `BOT_TOKEN` и вставь новый токен Discord-бота.
6. Если панель просит файл запуска, укажи `bot.py`.
7. Если панель просит команду, укажи `python -u bot.py`.
8. Сохрани настройки и запусти контейнер.

Не вставляй токен в исходный код или публичный архив. Приоритет токена: `BOT_TOKEN`, затем `DISCORD_TOKEN`, затем `TOKEN`. Для Bothost используй `BOT_TOKEN`.

## Discord Developer Portal
В приложении бота → Bot включи:
- Server Members Intent
- Message Content Intent

Приглашение бота должно содержать scopes `bot` и `applications.commands`. Для функций сервера выдай необходимые права, в том числе Manage Roles, Manage Channels, Send Messages, View Channels, Read Message History, Embed Links, Attach Files, Moderate Members, Kick Members, Ban Members и Manage Events.

## После запуска
В логах должна появиться строка `Запущен как ...`. Если снова появится `Improper token has been passed` или `401 Unauthorized`, это уже ошибка токена, а не установки библиотек.

Для сохранения настроек и базы после пересборки контейнера проверь, что Bothost сохраняет папку `data` как постоянное хранилище.
