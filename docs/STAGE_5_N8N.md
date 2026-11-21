# Ежедневный отчёт в n8n

**EduFlow — Daily Sales Report** запускается в 09:00 по Москве и запрашивает отчёт за предыдущий завершённый календарный день. Ручной запуск использует 01.06.2026–31.08.2026 со срезом на 31.10.2026. Готовый отчёт находится в **Executions → Daily sales report**, результат доставки — в **Delivery receipt**. [Подключение Telegram](STAGE_5_TELEGRAM.md)

## Первый запуск

Выполняйте команды из корня проекта в PowerShell. Нужны Docker Compose, uv и локальный `.env` с параметрами PostgreSQL. При первом запуске скопируйте `.env.example` в `.env` и заполните `POSTGRES_PASSWORD`. Для этой автоматизации достаточно параметров PostgreSQL и, при необходимости, `N8N_LOCAL_PORT`. [Настройка PostgreSQL](../README.md#локальный-postgresql-16)

```powershell
$compose = @('-f', 'docker-compose.yml', '-f', 'compose.n8n.yaml')
uv sync --locked
docker compose @compose up -d --wait postgres
uv run --locked alembic upgrade head
docker compose @compose up -d --build --wait --wait-timeout 120 n8n
docker compose @compose ps
```

Для воспроизведения демонстрационных сумм используйте базу со стандартным набором `synthetic_42`. В новой пустой базе выполните две команды. В базе с другими данными результат будет отражать фактические продажи.

```powershell
uv run --locked python -m scripts.generate_synthetic_data
uv run --locked python -m scripts.import_sales_data data/synthetic/generated --source-system synthetic_42
```

Откройте [локальный n8n](http://localhost:5678). При первом входе создайте локальную учётную запись владельца. Если порт занят, задайте другой `N8N_LOCAL_PORT` в `.env` и повторите `docker compose @compose up -d n8n`. В контейнере порт всегда 5678.

## Ручная проверка и публикация

Один раз импортируйте экспорт. В редакторе откройте **EduFlow — Daily Sales Report** и выберите **Execute Workflow** с триггером **Manual demo**. Для проверки из терминала доступны те же узлы.

```powershell
docker compose @compose exec -T n8n n8n import:workflow --input=/opt/eduflow/daily_sales_report.json
docker compose @compose exec -T -e N8N_RUNNERS_BROKER_PORT=5681 n8n n8n execute --id=EduFlowDailySales1
```

Отдельный порт брокера нужен только второму процессу n8n при ручной проверке из CLI. На стандартном наборе ожидаются 8 506 500,00 ₽ успешных оплат, 105 оплативших лидов, средний чек 81 014,29 ₽ и конверсия 39,33 %. После успешной проверки нажмите **Publish** в редакторе или выполните команду.

```powershell
docker compose @compose exec -T n8n n8n publish:workflow --id=EduFlowDailySales1
docker compose @compose exec -T n8n n8n list:workflow --active=true
```

Правило `0 9 * * *` использует `Europe/Moscow`. Даты вычисляются по метке времени триггера. Начало, конец и срез совпадают со вчерашней датой. Docker должен работать в момент запуска. После перезапуска n8n восстанавливает опубликованный workflow, настройки и журнал из тома `n8n_data`. Повторный импорт нужен при осознанном обновлении экспорта и может заменить изменения одноимённой версии с тем же ID. [Экспорт workflow](../workflows/daily_sales_report.json), [публикация через CLI n8n](https://docs.n8n.io/hosting/cli-commands/), [Schedule Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/)

## Данные и ошибки

Узел **Fetch SQL metrics** вызывает `GET http://report-api:8000/api/v1/metrics`. Внутренний FastAPI использует тот же обработчик `sales_metrics` и тот же SQL, что и основной API. В OpenAPI внутреннего сервиса доступен только маршрут чтения метрик. Ответ для журнала содержит период, четыре KPI и доступные разбивки. Менеджеры обозначены UUID. Пять кодов источников стандартного набора отображаются как есть, прочие свободные названия заменяются стабильными псевдонимами. Порт API остаётся внутри сети Compose. Контейнер n8n получает доступ только к сети отчётов, без настроек подключения PostgreSQL или Groq.

В узле **Report period** можно задать подготовленный код источника `source` и/или UUID `manager_id`. По умолчанию оба равны `null`, в HTTP-запрос попадают только даты. Для фильтрации по источнику используется исходный код из учётной системы. Вносите в настройки workflow только несекретные коды. После редактирования проверьте результат и опубликуйте версию.

Итоговый JSON содержит `period`, `filters`, `total`, непустые разбивки, `report_id` и текст для чтения. Десятичные суммы сохраняются строками. Нулевые оплаты показываются как 0,00 ₽, неопределённый средний чек или конверсия — «нет данных». Один набор дат и фильтров даёт один `report_id`. Повтор при тех же данных воспроизводит тот же отчёт. Каждый запуск сохраняет собственную запись аудита. В экспортной конфигурации отправка выключена.

На HTTP-запрос отведено пять секунд. После сетевой ошибки, таймаута, HTTP 408, 429, 500, 502, 503 или 504 возможны ещё две попытки с паузой две секунды. Иные статусы, повреждённый JSON, неожиданные даты и неверные типы KPI завершают выполнение с ошибкой. В **Executions** видны неудачный узел, причина и попытки. Диагностика валидатора исключает тело ответа. При сбое соединения проверьте `docker compose @compose ps`, затем `uv run --locked python -m scripts.check_db`.

## Приёмка

Проверки выполняются на стандартной демонстрационной базе. Тест поднимает одноразовый экземпляр n8n с отдельной SQLite в его временном каталоге. Он сверяет ручной отчёт с API и эталонными KPI, имитирует ошибки, затем публикует копию workflow с ускоренным расписанием и читает сохранённый результат. Рабочее расписание продолжает действовать в 09:00.

```powershell
docker compose @compose run --rm --no-deps -T -v ./tests/n8n/check_workflow.mjs:/opt/eduflow/check_workflow.mjs:ro --entrypoint node n8n /opt/eduflow/check_workflow.mjs
uv run --locked python -m pytest --integration -q -ra -W error
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Для остановки автоматизации используйте `docker compose @compose stop telegram-delivery n8n report-api`. [Результат приёмки](STAGE_5_TEST_REPORT.md)
