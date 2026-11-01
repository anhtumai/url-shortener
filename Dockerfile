FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PATH="/code/.venv/bin:$PATH"

WORKDIR /code

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# Don't run as root. Aiven reads the port from EXPOSE.
RUN useradd --system --no-create-home app
USER app

EXPOSE 8000

CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]
