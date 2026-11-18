# EduFlow AI

EduFlow AI — проект платформы аналитики и автоматизации продаж онлайн-школы. Данные о заявках, вводных уроках, банковских заявках и оплатах часто хранятся в разных системах. Проект призван свести эти данные вместе, чтобы руководитель мог оценивать воронку, сравнивать менеджеров и источники лидов, а также готовить отчёты по единым правилам.

## Текущее состояние

Подготовлены структура репозитория, минимальное приложение FastAPI и локальный PostgreSQL 16 через Docker Compose. Маршрут `GET /health` проверяет доступность HTTP-приложения. Подключение к базе проверяется отдельной командой. Статусы и критерии приёмки ведутся в [списке задач](docs/TASKS.md).

## Планируемые возможности

- Импорт синтетических данных из CSV и Excel и загрузка через REST API с валидацией, обнаружением дубликатов и отчётом об ошибках.
- Расчёт KPI по периодам, менеджерам и источникам и просмотр воронки, графиков и таблиц в Streamlit.
- Классификация обезличенных причин отказов и подготовка сводок по уже рассчитанным метрикам.
- Регулярное формирование отчётов через n8n и отправка в Telegram.

## Стек и архитектура

| Задача | Инструменты проекта |
|---|---|
| Язык, API и конфигурация | Python 3.12, FastAPI, Uvicorn, Pydantic, pydantic-settings |
| Хранение и миграции | PostgreSQL 16, SQLAlchemy 2.x, psycopg 3, Alembic |
| Аналитика и интерфейс | SQL, pandas по мере необходимости, Streamlit |
| Окружение, проверки и инфраструктура | uv, pytest, Ruff, Git/GitHub, Docker Compose |
| Последующие этапы | LLM API, Structured Outputs / JSON Schema, n8n, Telegram Bot API |

Backend — модульный монолит. PostgreSQL хранит события продаж, слой аналитики рассчитывает метрики, FastAPI отдаёт их через API, а Streamlit отображает результаты. LLM получает рассчитанные показатели и обезличенный текст. Подробнее в [архитектуре](docs/ARCHITECTURE.md).

## Структура репозитория

| Путь | Назначение |
|---|---|
| `app/` | API, настройки, подключение к БД, модели, схемы, аналитика, бизнес-сервисы и AI-модуль |
| `dashboard/`, `workflows/` | Интерфейс Streamlit и сценарии n8n |
| `data/synthetic/`, `scripts/` | Синтетические наборы и вспомогательные команды |
| `sql/queries/`, `migrations/versions/` | Аналитические запросы и версии миграций Alembic |
| `tests/unit/`, `tests/integration/` | Модульные и интеграционные тесты |
| `docs/` | Спецификация, архитектура, модель данных, KPI и ход работ |

## Локальный запуск

Для текущей версии нужны Git, uv и Python 3.12. Если подходящего Python нет, uv может загрузить его при создании окружения. Установите uv по [официальной инструкции](https://docs.astral.sh/uv/getting-started/installation/).

1. Откройте терминал в корне проекта и установите зависимости в `.venv`.

   ```powershell
   uv --version
   uv sync --locked
   uv run --locked python --version
   ```

   Ожидаемая версия Python — 3.12.x. `pyproject.toml` перечисляет прямые зависимости, а `uv.lock` фиксирует конкретные версии всего дерева зависимостей. Команды `uv run` запускают инструменты из окружения проекта.

2. Запустите API.

   ```powershell
   uv run --locked python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

3. Оставьте сервер запущенным и проверьте ответ во втором терминале. В Windows PowerShell используйте команду ниже.

   ```powershell
   Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' | Select-Object StatusCode, Content
   ```

   В Linux и macOS используйте `curl -i http://127.0.0.1:8000/health`. Ожидаемый результат — HTTP 200 и `{"status":"ok"}`. Документация API доступна на `http://127.0.0.1:8000/docs`. Чтобы остановить сервер, нажмите Ctrl+C в его терминале. Если порт 8000 занят, подставьте другой свободный порт в обе команды.

### Тесты и проверки

Тест обращается к маршруту через FastAPI TestClient. Он проверяет HTTP-код и JSON-ответ. Для запуска теста на уровне приложения хватает TestClient и его HTTP-клиента `httpx2`. [Документация TestClient](https://starlette.dev/testclient/)

Запустите команды из корня проекта.

```powershell
uv run --locked python -m pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

## Локальный PostgreSQL 16

Нужен запущенный Docker с командой `docker compose`. На Windows подойдёт Docker Desktop с Linux-контейнерами. Все команды выполняются из корня проекта.

1. При первом запуске скопируйте шаблон и задайте пароль в `.env`. Шаблон хранится в Git, а локальный `.env` игнорируется.

   ```powershell
   if (-not (Test-Path -LiteralPath '.env')) {
       Copy-Item -LiteralPath '.env.example' -Destination '.env'
   }
   ```

   Заполните `POSTGRES_PASSWORD` собственным паролем. `POSTGRES_DB` и `POSTGRES_USER` задают имя базы и пользователя. Для Python на хосте используются `POSTGRES_HOST=127.0.0.1` и `POSTGRES_PORT=5432`. Если порт занят, задайте в `.env` свободный порт, например `5433`.

2. Проверьте конфигурацию, запустите PostgreSQL и дождитесь готовности.

   ```powershell
   docker compose config --quiet
   docker compose up --wait --wait-timeout 90 postgres
   docker compose ps postgres
   ```

   В `docker compose ps postgres` ожидается статус `healthy`. Docker публикует порт на `127.0.0.1`. Каталог данных PostgreSQL хранится в именованном томе `eduflow-ai_postgres_data`. [Документация образа PostgreSQL](https://hub.docker.com/_/postgres)

3. Проверьте соединение из Python.

   ```powershell
   uv sync --locked
   uv run --locked python -m scripts.check_db
   ```

   Команда выполняет `SHOW server_version` и выводит версию PostgreSQL и сообщение `is reachable`. При ошибке она возвращает код 1 и предлагает проверить настройки или состояние контейнера.

4. Для остановки и повторного запуска используйте следующие команды.

   ```powershell
   docker compose stop postgres
   docker compose up --wait --wait-timeout 90 postgres
   uv run --locked python -m scripts.check_db
   ```

   Команды сохраняют именованный том с данными. Образ применяет `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD` при инициализации пустого тома. Для существующего тома указывайте текущие реквизиты базы. При проблеме запуска проверьте `docker compose ps postgres` и `docker compose logs --tail 40 postgres`. [Переменные официального образа](https://hub.docker.com/_/postgres)

## Данные и пример отчёта

На TASK-1.4 спроектирована схема для менеджеров, лидов, уроков, банковских заявок и оплат. Первая миграция запланирована на TASK-1.5. В первой версии используются синтетические данные. Бизнес-даты отображаются в часовом поясе Москвы. ER-диаграмма и ограничения приведены в [модели данных](docs/DATA_MODEL.md).

Пример будущего отчёта. Из двух лидов с первым проведённым уроком в выбранном периоде один отправил банковскую заявку и внёс два платежа по 30 000 ₽. Конверсия в заявку и в оплату составит по 50%. Сумма успешных оплат составит 60 000 ₽, средний оплаченный чек за тот же период — 60 000 ₽ на одного платящего лида. Полные правила приведены в [справочнике KPI](docs/KPI_DEFINITIONS.md).

## Документация

- [Контекст проекта](docs/PROJECT_CONTEXT.md)
- [Дорожная карта](docs/ROADMAP.md)
- [Задачи и критерии приёмки](docs/TASKS.md)
- [Технические решения](docs/DECISIONS.md)
- [История изменений](docs/CHANGELOG.md)
