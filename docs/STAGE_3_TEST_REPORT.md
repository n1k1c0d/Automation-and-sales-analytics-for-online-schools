# Отчёт о проверке этапа 3

Аудит проведён 19 ноября 2026 года в ветке `dev`. Исходный прогон сделан на коммите `915f5e4` с завершённой TASK-3.2. Итоговый прогон включает изменения TASK-3.3.

| Среда | Исходный набор | Итоговый набор |
|---|---|---|
| Windows, Python 3.12.13, uv 0.11.0, PostgreSQL 16.16 в локальном Docker | `72 passed in 37.76s` | `75 passed in 48.42s` |
| Отдельный Linux-контейнер, Python 3.12.16, uv 0.13.9, PostgreSQL 16.16 в выделенной сети | `72 passed in 22.23s` | `75 passed in 23.58s` |

Оба набора прошли без пропусков с `--integration` и `-W error`. На обеих платформах прошли линтинг и проверка форматирования Ruff. Alembic показал `0002 (head)` и подтвердил соответствие схемы моделям. В Linux миграции применены с пустой базы, тесты создали и удалили временные базы PostgreSQL.

Зависимости установлены по `uv.lock`. Использованы FastAPI 0.163.2, Uvicorn 0.54.0, Streamlit 1.67.0, Altair 6.3.0, Pydantic 2.14.3, SQLAlchemy 2.1.9, psycopg 3.3.9, Alembic 1.20.1, pytest 9.2.0 и Ruff 0.17.5. Для независимого Linux-прогона использован образ `ghcr.io/astral-sh/uv:python3.12-trixie-slim` с дайджестом `sha256:b12436aceed37b21524b327362bd67e73a284b5d52f4f6be0e047dd8e4c4eb90`.

## Что подтверждено

| Проверка | Результат |
|---|---|
| HTTP на запущенном Uvicorn | `/health` и `/docs` вернули HTTP 200. В OpenAPI присутствуют маршруты отчёта, справочников и импорта. |
| Сравнение REST API и CLI | JSON отчётов совпал для текущего и сравнительного периодов. Все 12 KPI текущего периода совпали с эталоном. Срез сравнения по умолчанию равен последнему дню сравниваемого периода. |
| Валидация среза | Срез раньше конца текущего периода и срез раньше конца периода сравнения вернули HTTP 422. CLI при раннем срезе вернул код 2. |
| Справочники фильтров | `GET /api/v1/dimensions` вернул 8 менеджеров и 5 каналов на стандартном наборе. Интеграционный тест дополнительно подтвердил, что в справочнике есть менеджер без лидов и канал, по которому события были только в предыдущем месяце. |
| Streamlit в браузере | После ввода среза 31 июля для периода, заканчивающегося 31 августа, интерфейс показал предупреждение. При выборе 1 июня в списке остались все 8 менеджеров. У Demo Manager 02 за этот день было 0 событий, интерфейс сохранил выбор и показал сообщение об отсутствии данных. |
| Streamlit и PostgreSQL в интеграционном тесте | Стандартный набор дал 167 лидов в июне и 165 в июле. Виджеты передали в REST API оба периода, даты срезов, идентификатор менеджера и канал. |
| Импорт | Полный набор тестов подтвердил загрузку CSV и Excel, частичный успех, повторы и обработку ошибок. |

Эталонные числа сохранены в [отчёте этапа 2](STAGE_2_TEST_REPORT.md#эталонные-kpi). Сценарии проверки находятся в [HTTP-тестах](../tests/unit/test_sales_api_validation.py) и [интеграционных тестах дашборда](../tests/integration/test_sales_dashboard.py).

## Повтор проверки

Запустите PostgreSQL и подготовьте `.env` по [инструкции проекта](../README.md#локальный-postgresql-16). Выполните команды из корня проекта.

```powershell
uv sync --locked
uv run --locked python -m scripts.check_db
uv run --locked alembic upgrade head
uv run --locked alembic current
uv run --locked alembic check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m pytest --integration -q -ra -W error
```

Для ручной проверки загрузите [стандартный набор](../README.md#импорт-csv-и-excel) и запустите API.

```powershell
uv run --locked python -m uvicorn app.main:app --host 127.0.0.1 --port 18033
```

Во втором терминале проверьте справочники, итог отчёта и коды ответа для раннего среза первого и второго периода.

```powershell
$api = 'http://127.0.0.1:18033'
Invoke-RestMethod "$api/health"
Invoke-RestMethod "$api/api/v1/dimensions"
$dates = 'start_date=2026-06-01&end_date=2026-08-31'
$comparison = 'compare_start_date=2026-06-01&compare_end_date=2026-06-30'
$report = Invoke-RestMethod "$api/api/v1/metrics?${dates}&cutoff_date=2026-10-31&$comparison"
$report.current.total
(Invoke-WebRequest "$api/api/v1/metrics?${dates}&cutoff_date=2026-05-01" -SkipHttpErrorCheck).StatusCode
(Invoke-WebRequest "$api/api/v1/metrics?${dates}&${comparison}&compare_cutoff_date=2026-06-29" -SkipHttpErrorCheck).StatusCode
```

Запустите дашборд во втором терминале и откройте `http://127.0.0.1:18533`.

```powershell
$env:EDUFLOW_API_URL = 'http://127.0.0.1:18033'
uv run --locked python -m streamlit run dashboard/main.py --server.address 127.0.0.1 --server.port 18533
```

Для проверки пустой выборки выберите 1 июня 2026 года как начало и конец периода, затем выберите Demo Manager 02.

Публичное развёртывание требует авторизации импорта, раздельных прав чтения и записи, политики доступа к Swagger и тестов отказа в доступе. [Требования к публичному запуску](API.md#перед-публичным-развёртыванием)
