# Отчёт о проверке TASK-1.7

Дата проверки — 18 ноября 2026 года. Исходный набор соответствует коммиту `e069f4b`. Расширенный набор включает его 8 тестов и 14 новых проверок в [test_sales_constraints.py](../tests/integration/test_sales_constraints.py).

| Среда | Исходный прогон | Расширенный прогон |
|---|---|---|
| Windows, Python 3.12.13, uv 0.11.0, PostgreSQL 16.16 | `8 passed in 2.63s` | `22 passed in 4.28s` |
| Отдельный Linux-контейнер, Python 3.12.16, uv 0.13.9, отдельный PostgreSQL 16.16 | `8 passed in 2.12s` | `22 passed in 1.91s` |

Во всех прогонах использован pytest 9.2.0 и флаг `--integration`. Пропущенных тестов и ошибок в этих прогонах не было. Начальный прогон Windows выполнен до добавления новых тестов. Прогон Linux выбрал исходные файлы перед запуском полного набора.

В Linux использован образ `ghcr.io/astral-sh/uv:python3.12-trixie-slim` с дайджестом `sha256:b12436aceed37b21524b327362bd67e73a284b5d52f4f6be0e047dd8e4c4eb90`. Зависимости установлены по `uv.lock` в чистое окружение. Контейнер PostgreSQL запущен на выделенной сети Docker без опубликованного порта. По завершении проверено отсутствие временных баз pytest, затем проверочные контейнеры и сеть удалены.

## Подтверждённые проверки

- Исходный набор проверил HTTP-маршрут, валидацию менеджера, запись и чтение из разных сессий, поиск отсутствующего менеджера и откат записи после ошибки ограничения.
- Новые тесты проверили связь лида с менеджером, сохранение допустимых событий, отклонение неизвестных статусов, несовместимых со статусом дат, нулевой и отрицательной суммы и `NaN`.
- На Windows и в Linux `ruff check .` вернул `All checks passed!`, `ruff format --check .` вернул `36 files already formatted`.
- Alembic в обеих средах показал `0001 (head)` и `No new upgrade operations detected.`. В отдельном контейнере миграция применена к пустой базе перед проверкой.
- В индексе Git отсутствуют виртуальные окружения, кэши Python, pytest, Ruff и mypy, байткод и локальный `.env`. Семь контрольных путей успешно прошли `git check-ignore`. `.env.example` и `uv.lock` отслеживаются.

## Воспроизведение

Подготовьте `.env` и запустите PostgreSQL по [README](../README.md). В корне проекта выполните команды.

```powershell
uv sync --locked
uv run --locked alembic current
uv run --locked alembic check
uv run --locked python -m pytest --integration -q -ra -W error tests/unit tests/integration/test_managers.py
uv run --locked python -m pytest --integration -q -ra -W error
uv run --locked ruff check .
uv run --locked ruff format --check .
git check-ignore --no-index -- .env .env.local .venv/pyvenv.cfg .pytest_cache/v/cache/nodeids .ruff_cache/example .mypy_cache/3.12/example app/__pycache__/main.cpython-312.pyc
```

Для новой локальной базы сначала выполните `uv run --locked alembic upgrade head`. Полный pytest создаёт собственную временную базу и применяет к ней миграцию. Обычный pytest без `--integration` выполняет модульные тесты и пропускает сценарии PostgreSQL. Отдельный запуск в этом режиме с заведомо недоступным адресом PostgreSQL дал `4 passed, 18 skipped in 0.83s`.
