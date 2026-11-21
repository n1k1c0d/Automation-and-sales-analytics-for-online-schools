# EduFlow AI

EduFlow AI — проект платформы аналитики и автоматизации продаж онлайн-школы. Данные о заявках, вводных уроках, банковских заявках и оплатах часто хранятся в разных системах. Проект призван свести эти данные вместе, чтобы руководитель мог оценивать воронку, сравнивать менеджеров и источники лидов, а также готовить отчёты по единым правилам.

## Текущее состояние

Для первого запуска используйте инструкцию от пустой базы до демо. Готовность сервисов и совпадение 12 KPI между API и SQL проверяет команда `uv run --locked python -m scripts.smoke_stage6 --expect-leads 500`. [Локальное развёртывание](docs/DEPLOYMENT.md), [результаты end-to-end проверки](docs/STAGE_6_TEST_REPORT.md)

FastAPI принимает CSV и Excel и возвращает KPI из PostgreSQL. Дашборд Streamlit показывает карточки KPI, динамику, воронку и срезы. Генератор, импорт и отчёт также доступны из командной строки. [Контракт API](docs/API.md), [запуск дашборда](#дашборд-streamlit), [список задач](docs/TASKS.md)

AI-команды используют GroqCloud с моделью `qwen/qwen3.8-27b`. Модель выбирает причину отказа или нужный KPI. Суммы, конверсии и лидирующие источники приложение определяет по отчёту PostgreSQL. [Запуск AI-команд](#ai-команды-groqcloud)

n8n формирует ежедневный отчёт в 09:00 по Москве. После подключения бота отчёт можно отправлять одному разрешённому получателю в Telegram. [Настройка доставки](docs/STAGE_5_TELEGRAM.md)

## Стек и архитектура

| Задача | Инструменты проекта |
|---|---|
| Язык, API и конфигурация | Python 3.12, FastAPI, Uvicorn, Pydantic, pydantic-settings |
| Хранение и миграции | PostgreSQL 16, SQLAlchemy 2.x, psycopg 3, Alembic |
| Аналитика и интерфейс | SQL, Streamlit, Altair |
| Окружение, проверки и инфраструктура | uv, pytest, Ruff, Git/GitHub, Docker Compose |
| AI | GroqCloud, Qwen 3.8 27B, Chat Completions со строгой JSON Schema |
| Автоматизация | n8n, ежедневный отчёт через FastAPI, Telegram Bot API |

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

   Ожидаемая версия Python — 3.12.x. `pyproject.toml` перечисляет прямые зависимости, а `uv.lock` фиксирует конкретные версии всего дерева зависимостей. Проект использует режим копирования пакетов из кеша uv для совместимости с папками OneDrive в Windows. Команды `uv run` запускают инструменты из окружения проекта. [Режим установки пакетов в uv](https://docs.astral.sh/uv/reference/settings/#link-mode)

2. Запустите API.

   ```powershell
   uv run --locked python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

3. Оставьте сервер запущенным и проверьте ответ во втором терминале. В Windows PowerShell используйте команду ниже.

   ```powershell
   Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' | Select-Object StatusCode, Content
   ```

   В Linux и macOS используйте `curl -i http://127.0.0.1:8000/health`. Ожидаемый результат — HTTP 200 и `{"status":"ok"}`. Документация API доступна на `http://127.0.0.1:8000/docs`. Для импорта и отчётов сначала запустите PostgreSQL и примените миграции. Чтобы остановить сервер, нажмите Ctrl+C в его терминале. Если порт 8000 занят, подставьте другой свободный порт в обе команды. [Запросы импорта и KPI](docs/API.md)

### Тесты и проверки

Модульные тесты проверяют ответ `/health`, валидацию HTTP-запросов и правила обработки данных. Запустите команды из корня проекта.

```powershell
uv run --locked python -m pytest tests/unit
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Для полной проверки запустите PostgreSQL по инструкции ниже и выполните команду.

```powershell
uv run --locked python -m pytest --integration
```

Интеграционные тесты используют параметры сервера из `.env`. Пользователю PostgreSQL нужен доступ к служебной базе `postgres` и право создавать базы. Тестовый прогон создаёт временные базы `eduflow_test_<uuid>`, применяет к ним миграции Alembic и удаляет их при завершении. Тесты проверяют запись и чтение, откат транзакции, ограничения схемы, импорт CSV и Excel и расчёт KPI, в том числе через REST API. Отдельный тест импортирует стандартные 500 лидов и сравнивает все 12 KPI с эталоном. Обычный запуск pytest пропускает проверки PostgreSQL. С флагом `--integration` недоступность PostgreSQL приводит к ошибке.

Результаты проверок сохранены в отчётах [первого этапа](docs/TEST_REPORT.md), [второго этапа](docs/STAGE_2_TEST_REPORT.md), [третьего этапа](docs/STAGE_3_TEST_REPORT.md) и [четвёртого этапа](docs/STAGE_4_TEST_REPORT.md).

## Локальный PostgreSQL 16

Нужен запущенный Docker с командой `docker compose`. На Windows подойдёт Docker Desktop с Linux-контейнерами. Все команды выполняются из корня проекта.

1. При первом запуске скопируйте шаблон и задайте пароль в `.env`. Шаблон хранится в Git, а локальный `.env` игнорируется.

   ```powershell
   if (-not (Test-Path -LiteralPath '.env')) {
       Copy-Item -LiteralPath '.env.example' -Destination '.env'
   }
   ```

   Заполните `POSTGRES_PASSWORD` собственным паролем. `POSTGRES_DB` и `POSTGRES_USER` задают имя базы и пользователя. Для Python на хосте используются `POSTGRES_HOST=127.0.0.1` и `POSTGRES_PORT=5432`. Если порт занят, задайте в `.env` свободный порт, например `5433`.

   Для AI-команд также заполните `GROQ_API_KEY` в локальном `.env`. В шаблоне уже указаны модель и лимиты. [Настройки Groq](docs/LLM.md#настройка)

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

## Миграции базы данных

Запустите PostgreSQL и выполните `uv sync --locked`. Для просмотра SQL всех миграций от пустой схемы выполните команду ниже. Alembic выведет SQL в терминал в автономном режиме. [Генерация SQL в Alembic](https://alembic.sqlalchemy.org/en/latest/offline.html)

```powershell
uv run --locked alembic upgrade head --sql
```

Примените миграции к локальной базе из `.env` и проверьте результат.

```powershell
uv run --locked alembic upgrade head
uv run --locked alembic current
uv run --locked alembic check
```

Ожидаются ревизия `0002 (head)` и сообщение `No new upgrade operations detected.` Alembic записывает номер применённой ревизии в таблицу `alembic_version`. Правила предметной области проверяются ограничениями PostgreSQL.

### Проверка отката

В PowerShell создайте отдельную временную базу. В примере откатывается её схема, а в конце удаляется только эта временная база.

```powershell
$testDb = 'eduflow_migration_' + [Guid]::NewGuid().ToString('N')
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" -- "$1"' sh $testDb
if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать тестовую базу.' }

$previousDb = $env:POSTGRES_DB
try {
    $env:POSTGRES_DB = $testDb
    uv run --locked alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Применение миграций завершилось ошибкой.' }
    uv run --locked alembic downgrade base
    if ($LASTEXITCODE -ne 0) { throw 'Откат завершился ошибкой.' }
    uv run --locked alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Повторная миграция завершилась ошибкой.' }
    uv run --locked alembic check
    if ($LASTEXITCODE -ne 0) { throw 'Схема требует проверки.' }
} finally {
    $env:POSTGRES_DB = $previousDb
    docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" -- "$1"' sh $testDb
}
```

## Генерация синтетических данных

В корне проекта выполните команду.

```powershell
uv run --locked python -m scripts.generate_synthetic_data
```

По умолчанию команда использует `seed=42`, создаёт 500 лидов за период с 1 июня по 29 августа 2026 года и записывает пять CSV в `data/synthetic/generated/`.

| Файл | Строк данных |
|---|---:|
| `managers.csv` | 8 |
| `leads.csv` | 500 |
| `lessons.csv` | 481 |
| `bank_applications.csv` | 209 |
| `payments.csv` | 196 |

Набор содержит разные источники лидов, повторные уроки, повторные банковские заявки, оплаты частями и возвраты. CSV записан в UTF-8 с запятой как разделителем. Даты имеют смещение `+00:00`. Суммы записаны с двумя знаками после точки. Формат ключей описан в [модели данных](docs/DATA_MODEL.md#формат-синтетических-файлов).

Для другого объёма, периода или источника выберите параметры и папку. `--start-date` задаёт первый день 90-дневного окна поступления лидов. Уроки, заявки и оплаты могут приходиться на более поздние даты.

```powershell
uv run --locked python -m scripts.generate_synthetic_data --leads 100 --seed 43 --start-date 2026-09-01 --output-dir data/synthetic/scenarios/seed-43
```

Одинаковые аргументы воспроизводят те же записи и UUID. При повторном запуске папка готового набора должна содержать ровно пять ожидаемых CSV с тем же содержимым. При отличиях, лишних файлах или вложенных папках команда просит указать другой путь. Для отдельного источника используйте новый `seed`. Git игнорирует папки `data/synthetic/generated/` и `data/synthetic/scenarios/`.

### Сценарий с различиями в конверсии

```powershell
uv run --locked python -m scripts.generate_synthetic_data --profile data/synthetic/profiles/quality.json
```

Команда записывает пять CSV в `data/synthetic/scenarios/quality-42/` с кодом источника `synthetic_quality_42`. В [профиле](data/synthetic/profiles/quality.json) менеджер `M01` и канал `referral` повышают вероятности, а `M08` и `paid_search` понижают их.

Профиль задаёт целочисленные поправки в процентных пунктах для проведения назначенного урока, появления банковской заявки и успешной оплаты после первого проведённого урока. Для оплаты базовая вероятность зависит от наличия одобрения банка. Поправки менеджера и канала складываются, итоговая вероятность ограничивается диапазоном от 0 до 100%. Поправка для менеджера или канала без записи в профиле равна нулю. Исходный набор с кодом `synthetic_42` воспроизводится командой без профиля.

Для анализа сценария используйте отдельную базу. SQL-отчёт без фильтров агрегирует лиды из всех импортированных наборов.

```powershell
uv run --locked python -m scripts.import_sales_data data/synthetic/scenarios/quality-42 --source-system synthetic_quality_42
```

## Импорт CSV и Excel

После запуска PostgreSQL установите зависимости и примените миграции. Для загрузки стандартного набора выполните команды из корня проекта.

```powershell
uv sync --locked
uv run --locked alembic upgrade head
uv run --locked python -m scripts.import_sales_data data/synthetic/generated --source-system synthetic_42
```

Для Excel укажите путь к книге `.xlsx` и постоянный код источника. Например, книгу с синтетическими выгрузками можно положить в игнорируемую папку `data/local/`.

```powershell
uv run --locked python -m scripts.import_sales_data data/local/sales.xlsx --source-system demo_crm --excel-timezone Europe/Moscow
```

Входом может быть папка CSV, один CSV с именем таблицы или книга с листами по именам таблиц. Заголовки находятся в первой строке. Для загрузки части таблиц родительские записи должны присутствовать во входном наборе или уже существовать в том же источнике.

| CSV или лист Excel | Поля помимо обязательного `external_id` |
|---|---|
| `managers` | `name`, `team`, `created_at` |
| `leads` | `manager_external_id`, `source`, `created_at`, `status` |
| `lessons` | `lead_external_id`, `scheduled_at`, `status`, `held_at` для `completed` |
| `bank_applications` | `lead_external_id`, `status`, `submitted_at` для отправленных заявок |
| `payments` | `lead_external_id`, `amount_rub`, `status`, `paid_at` для `paid` и `refunded` |

- CSV использует UTF-8 и запятую. Идентификаторы в Excel должны быть текстовыми. Даты можно указать текстом со смещением, например `2026-06-01T10:00:00+03:00`. Для ячеек даты Excel задайте часовой пояс через `--excel-timezone`. Если местное время попадает на неоднозначный переход часов, укажите смещение текстом. Сумму можно задать числом Excel или текстом с точкой и точностью до копеек.
- Значения в листах должны быть обычными ячейками. Формулы и ячейки с ошибкой отклоняются. Коды источников состоят из 1–64 строчных латинских букв, цифр или подчёркиваний. Внешние ID имеют длину 1–255 символов. Столбец `source_system` необязателен. Если значение указано, оно должно совпасть с `--source-system`.
- PostgreSQL присваивает внутренние UUID. Поля `id`, `manager_id` и `lead_id` из генератора допустимы как справочные колонки. Родительские строки определяются по внешним ID. Подробности и ограничения описаны в [модели данных](docs/DATA_MODEL.md#идентификаторы-для-повторного-импорта).

Команда выводит JSON со счётчиками по таблицам. `inserted` означает новую запись, `duplicate` — совпавший повтор. `conflict` означает, что запись с тем же ключом уже есть и её данные отличаются. `invalid` показывает ошибку в строке. Для конфликтов и ошибок указаны таблица, номер строки и причина.

Код выхода `0` означает обработку всех строк без ошибок, `1` — наличие ошибок или конфликтов при сохранённых корректных строках, `2` — сбой формата файла, настроек или PostgreSQL. При сбое всего импорта транзакция откатывается. Повтор стандартной загрузки после успешного импорта тех же файлов даёт 1 394 дубликата и сохраняет существующие UUID.

## Отчёт по продажам

После импорта стандартного синтетического набора выполните команду.

```powershell
uv run --locked python -m scripts.sales_report --start-date 2026-06-01 --end-date 2026-08-31 --cutoff-date 2026-10-31
```

Отчёт возвращает JSON с итогом `total`, разбивкой по дням `daily`, менеджерам `by_manager` и каналам `by_source`. Обе даты периода включены по московскому времени. Срез включён и ограничивает исходы когортных конверсий. По умолчанию он совпадает с последней датой периода. Суммы и проценты передаются строками с десятичной точностью.

На стандартном наборе этот период даёт 500 входящих лидов, 335 проведённых уроков и 8 506 500,00 ₽ успешных оплат. В когорте 300 лидов. Конверсия в банковскую заявку к 31 октября составляет 54,00%, в оплату — 39,33%. [Все 12 эталонных значений](docs/STAGE_2_TEST_REPORT.md#эталонные-kpi)

Для фильтрации используйте `--source webinar` или `--manager-id UUID`. UUID менеджера можно взять из `by_manager`. Фильтры можно комбинировать. Для сравнения периодов используйте `--compare START END` и, при необходимости, `--compare-cutoff-date`.

```powershell
uv run --locked python -m scripts.sales_report --start-date 2026-07-01 --end-date 2026-07-31 --cutoff-date 2026-08-31 --compare 2026-06-01 2026-06-30 --compare-cutoff-date 2026-07-31 --source webinar
```

При сравнении команда возвращает два отчёта, `current` и `comparison`. Каждый рассчитывается по своему периоду и срезу с одинаковыми фильтрами. Формулы приведены в [справочнике KPI](docs/KPI_DEFINITIONS.md).

## Ежедневный отчёт в n8n

n8n получает SQL-показатели через FastAPI и записывает отчёт в **Executions**. Расписание — ежедневно в 09:00 по Москве за предыдущий день. Для ручной проверки предусмотрен исторический период на стандартных 500 лидах. [Настройка, импорт и публикация workflow](docs/STAGE_5_N8N.md)

## AI-команды GroqCloud

В PowerShell создайте файл с синтетическим комментарием. Команда `classify` принимает массив строк, а `evaluate` читает файл с эталонными метками. Для оценки по умолчанию используется основной набор и партии по два комментария. Каждая команда ниже обращается к GroqCloud и расходует квоту.

```powershell
New-Item -ItemType Directory -Force data/local | Out-Null
'["Банк отказал в рассрочке."]' | Set-Content -Encoding utf8 data/local/demo_comments.json
uv run --locked python -m scripts.ai_sales classify data/local/demo_comments.json
uv run --locked python -m scripts.ai_sales evaluate
uv run --locked python -m scripts.ai_sales evaluate data/synthetic/ai/rejection_challenge_cases.json --batch-size 2
```

После импорта стандартного набора сохраните SQL-отчёт и используйте его в обеих командах аналитики. [Подготовка данных и интерпретация ответов](docs/AI_ANALYSIS.md)

```powershell
uv run --locked python -m scripts.sales_report --start-date 2026-06-01 --end-date 2026-08-31 --cutoff-date 2026-10-31 |
  Set-Content -Encoding utf8 data/local/sales_report.json
uv run --locked python -m scripts.ai_sales summarize data/local/sales_report.json
uv run --locked python -m scripts.ai_sales ask data/local/sales_report.json 'Какой источник принёс больше всего денег?'
```

## Дашборд Streamlit

Поднимите PostgreSQL, примените миграции и [импортируйте стандартный набор](#импорт-csv-и-excel). Оставьте FastAPI запущенным на `127.0.0.1:8000`. Во втором терминале из корня проекта выполните команду.

```powershell
uv run --locked python -m streamlit run dashboard/main.py --server.address 127.0.0.1
```

Откройте `http://127.0.0.1:8501`. Если FastAPI запущен на другом порту, перед запуском Streamlit задайте переменную окружения `EDUFLOW_API_URL`, например `$env:EDUFLOW_API_URL = 'http://127.0.0.1:8001'`.

Начальный период с 1 июня по 31 августа 2026 года и срез на 31 октября совпадают с эталонным отчётом для 500 лидов. В боковой панели можно менять даты, дату среза, менеджера и канал. Срез задаётся на дату окончания периода или позже. Фильтры используют общие справочники, поэтому сохраняют менеджеров и каналы без событий в выбранном периоде. После импорта нажмите «Обновить данные», чтобы перечитать отчёт и справочники.

Для сравнения включите «Сравнить периоды». График сопоставляет дни от начала каждого периода, а карточки показывают абсолютную разницу. Изменение конверсий выражено в процентных пунктах. Для контрольного примера выберите июль 2026 года со срезом 31 октября и июнь 2026 года со срезом 31 августа. Карточка входящих лидов покажет 165 и разницу −2 относительно 167 лидов в июне. В разделе «Срезы» можно переключать период для таблиц каналов и менеджеров.

## Данные проекта

Схема включает менеджеров, лиды, уроки, банковские заявки и оплаты. В первой версии используются синтетические данные. Бизнес-даты отображаются в часовом поясе Москвы. ER-диаграмма и ограничения приведены в [модели данных](docs/DATA_MODEL.md).

Первый рабочий сценарий находится в [сервисе менеджеров](app/services/managers.py). `create_manager` удаляет пробельные символы по краям имени и команды, проверяет поля и получает UUID из PostgreSQL. `get_manager` ищет запись по UUID и возвращает `None`, если записи нет. Вызывающий код управляет транзакцией через `Session.begin()`. [Сессии и транзакции SQLAlchemy](https://docs.sqlalchemy.org/en/21/orm/session_basics.html)

## Документация

- [Контекст проекта](docs/PROJECT_CONTEXT.md)
- [REST API](docs/API.md)
- [Дорожная карта](docs/ROADMAP.md)
- [Задачи и критерии приёмки](docs/TASKS.md)
- [Технические решения](docs/DECISIONS.md)
- [История изменений](docs/CHANGELOG.md)
- [Отчёт о проверке второго этапа](docs/STAGE_2_TEST_REPORT.md)
- [Настройка и проверка LLM-клиента](docs/LLM.md)
- [AI-аналитика и примеры команд](docs/AI_ANALYSIS.md)
- [Итоги приёмки этапа 4](docs/STAGE_4_TEST_REPORT.md#stage-4-acceptance)
