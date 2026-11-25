FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY sql ./sql
COPY workflows/report_api.py ./workflows/report_api.py
CMD ["/service/.venv/bin/uvicorn", "workflows.report_api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
