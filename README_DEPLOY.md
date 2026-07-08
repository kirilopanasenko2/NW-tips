# Senko Tips Bot — Render + Supabase

Этот вариант работает на бесплатном Render через Telegram webhook.
Чаевые хранятся в PostgreSQL-базе Supabase и не пропадают при перезапуске сервера.

## Что понадобится

1. Аккаунт GitHub
2. Аккаунт Supabase
3. Аккаунт Render
4. Токен бота от BotFather

## Шаг 1. Создайте базу Supabase

1. Откройте Supabase и нажмите **New project**.
2. Название можно указать `senko-tips`.
3. Придумайте пароль базы данных и сохраните его.
4. Выберите ближайший регион.
5. После создания проекта нажмите **Connect**.
6. Выберите тип соединения **Transaction pooler**.
7. Скопируйте готовую строку подключения URI.

Она выглядит примерно так:

```text
postgresql://postgres.PROJECT_ID:PASSWORD@aws-0-us-west-1.pooler.supabase.com:6543/postgres
```

Это значение понадобится как `DATABASE_URL`.

Важно: используйте именно **Transaction pooler**, а не Direct connection.

Таблицу вручную создавать не нужно — бот создаст её сам при первом запуске.

## Шаг 2. Загрузите проект в GitHub

1. Создайте новый репозиторий на GitHub, например `senko-tips-bot`.
2. Распакуйте архив проекта.
3. На странице пустого репозитория нажмите **uploading an existing file**.
4. Перетащите туда содержимое папки проекта:
   - `bot.py`
   - `requirements.txt`
   - `render.yaml`
   - `.gitignore`
   - `.env.example`
5. Нажмите **Commit changes**.

Файл `.env` с настоящими паролями загружать нельзя.

## Шаг 3. Разверните на Render

1. Войдите на Render через GitHub.
2. Нажмите **New → Blueprint**.
3. Подключите репозиторий `senko-tips-bot`.
4. Render прочитает файл `render.yaml`.
5. Введите секретные переменные:

### BOT_TOKEN

Вставьте токен, который прислал BotFather.

### DATABASE_URL

Вставьте строку **Transaction pooler** из Supabase.

### ALLOWED_USER_IDS

Пока можно оставить пустым.
Позже впишите три Telegram ID через запятую:

```text
111111111,222222222,333333333
```

6. Нажмите создание/применение Blueprint.
7. Дождитесь строки в логах:

```text
Запуск webhook: https://...onrender.com/telegram
```

## Шаг 4. Остановите старую копию на Mac

После успешного запуска на Render остановите локального бота:

```text
Control + C
```

Одновременно должен работать только один экземпляр бота.

## Проверка

Откройте Telegram и нажмите **Мой чай** или **+$5**.

На бесплатном Render сервис засыпает после периода без запросов.
Первое сообщение после долгой паузы может отвечать медленнее, затем бот работает нормально.

## Локальный запуск обновлённой версии

Создайте `.env` из примера:

```bash
cp .env.example .env
```

Заполните `BOT_TOKEN` и `DATABASE_URL`, затем:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Если переменной `RENDER_EXTERNAL_URL` нет, бот автоматически запускается через polling.
