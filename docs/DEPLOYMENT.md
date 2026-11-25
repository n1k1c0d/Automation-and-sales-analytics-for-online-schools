# Локальный запуск EduFlow AI

Нужны Git, Docker Desktop с Compose и uv. Команды ниже рассчитаны на PowerShell из корня репозитория. Для запуска демо достаточно синтетических данных. Проверки готовности работают без аккаунтов Groq и Telegram.

## 1. Подготовить базу и данные

В чистой копии создайте `.env` командой `Copy-Item .env.example .env`. Задайте собственный `POSTGRES_PASSWORD`. В существующей копии используйте её настройки. Оставьте `POSTGRES_HOST=127.0.0.1`. Загружайте эталонный набор в пустую базу, чтобы остальные данные не влияли на итоговый отчёт.

```powershell
uv sync --locked
$compose = @('-f', 'docker-compose.yml', '-f', 'compose.n8n.yaml')
docker compose @compose up -d --wait postgres
uv run --locked python -m scripts.check_db
uv run --locked alembic upgrade head
uv run --locked alembic check
uv run --locked python -m scripts.generate_synthetic_data
uv run --locked python -m scripts.import_sales_data data/synthetic/generated --source-system synthetic_42
```

Ожидаются миграция `0002 (head)`, 500 лидов, 8 менеджеров, 481 урок, 209 банковских заявок и 196 оплат. Повтор импорта тех же файлов с тем же `source-system` распознаёт дубликаты. [Контракт импорта](DATA_IMPORT.md#импорт-csv-и-excel)

## 2. Запустить API, интерфейс и n8n

В текущем терминале выполните:

```powershell
docker compose @compose up -d --build --wait --wait-timeout 180 telegram-delivery
docker compose @compose ps
uv run --locked python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Во втором терминале из корня проекта запустите Streamlit:

```powershell
uv run --locked python -m streamlit run dashboard/main.py --server.address 127.0.0.1 --server.headless true
```

Откройте [дашборд](http://127.0.0.1:8501). Для стандартного периода 01.06–31.08.2026 со срезом 31.10.2026 ожидаются 500 лидов, 105 оплативших и сумма 8 506 500 ₽. При выборе источника `organic_search` сумма составляет 2 867 000 ₽. Основной FastAPI доступен на loopback, отдельный сервис `report-api` доступен n8n внутри Compose. [HTTP API](API.md)

В [локальном n8n](http://127.0.0.1:5678) при первом входе создайте учётную запись владельца. Один раз импортируйте экспорт с ID `EduFlowDailySales1`, запустите **Manual demo**, затем опубликуйте workflow. Экспорт поставляется с выключенной отправкой. При повторной установке поверх работающего экземпляра сначала сохраните его локальные настройки и привязки. [Команды импорта и публикации](STAGE_5_N8N.md#ручная-проверка-и-публикация)

Токен Telegram вводится в Credentials n8n. На новом экземпляре создайте Telegram API credential, задайте локальный Base URL `http://127.0.0.1:5687`, привяжите credential к узлу и настройте один разрешённый личный чат. Для восстановления существующего экземпляра сохраните том `n8n_data` с соответствующим ключом шифрования и отдельный `telegram_delivery_data` с журналом доставок. Потеря последнего снимает защиту от повторной отправки старого отчёта. [Пошаговая настройка и ручное подтверждение](STAGE_5_TELEGRAM.md)

Для ежедневной доставки настройте `EDUFLOW_TELEGRAM_CHAT_ID` в локальном `.env` и примените его командой `docker compose @compose up -d --wait telegram-delivery`. В редакторе включите **Telegram Send Message** с подготовленными Credentials. В **Prepare Telegram delivery** установите `sendScheduled = true`, проверьте у **Daily 09:00** cron `0 9 * * *` и в настройках workflow часовой пояс `Europe/Moscow`. Нажмите **Publish**. Ручная отправка управляется отдельным флагом `sendManual`.

После срабатывания откройте **Executions → Delivery receipt** и сверьте отчёт за предыдущий московский день с сообщением в чате. Для первой успешной отправки ожидайте `sent` и положительный `message_id`, для повтора уже доставленного отчёта — `already_sent` и ноль новых попыток. [Проверка на рабочем боте](STAGE_5_TEST_REPORT.md#подтверждение-доставки-по-расписанию)

## 3. Проверить готовность

В третьем терминале выполните:

```powershell
uv run --locked python -m scripts.smoke_stage6 --expect-leads 500
```

Каждая проверка выводит `PASS`, `FAIL` или `SKIP`. Код 0 означает успех включённых проверок, код 1 — сбой. Скрипт проверяет PostgreSQL, часовой пояс файла workflow, FastAPI, справочники, контракт отчёта, все 12 итоговых KPI по SQL, доступность Streamlit и n8n. Для другой базы опустите `--expect-leads 500`. Для сравнения API и SQL используйте одинаковую базу и период без параллельного импорта.

Для проверки только backend добавьте `--skip-streamlit --skip-n8n`. Альтернативные адреса задаются аргументами `--api-url`, `--streamlit-url`, `--n8n-url`. По умолчанию API учитывает `EDUFLOW_API_URL` из окружения процесса, n8n — `N8N_LOCAL_PORT` из окружения или `.env`. Опция `--workflow` позволяет проверить часовой пояс другого экспорта перед публикацией. Опция `--with-groq` включает реальный запрос с расходом квоты и требует `GROQ_API_KEY`. Telegram проверяют отдельно в контролируемом ручном запуске. [План и критерии проверки](STAGE_6_TEST_PLAN.md)

## Изолированный повтор развёртывания

Для репетиции используйте отдельную чистую копию, собственный `.env` из примера, свободные порты и отдельное имя Compose. Например, задайте `POSTGRES_PORT=55461`, `N8N_LOCAL_PORT=15671` и в начале команд используйте `$compose = @('-p','eduflow-s6-demo','-f','docker-compose.yml','-f','compose.n8n.yaml')`. Во всех терминалах запускайте Python из этой копии. Для API задайте `--port 18061`, для Streamlit — `--server.port 18561` и перед его запуском `$env:EDUFLOW_API_URL='http://127.0.0.1:18061'`. Передайте новые адреса в smoke-команду. Обычный перезапуск проверяется командой `docker compose @compose restart`, затем `docker compose @compose up -d --wait telegram-delivery` и повтором smoke. Для остановки используйте `docker compose @compose stop`. Именованные тома сохраняются.

## Локальный PostgreSQL 16

Docker публикует порт базы на `127.0.0.1` и сохраняет именованный том при обычной остановке. Для занятого порта задайте в `.env` свободный `POSTGRES_PORT`. Имя базы, пользователь и пароль применяются при первой инициализации пустого тома. Для существующего тома используйте его реквизиты.

```powershell
docker compose config --quiet
docker compose ps postgres
docker compose logs --tail 40 postgres
uv run --locked python -m scripts.check_db
```

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
