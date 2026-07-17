FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini start.sh ./
RUN pip install --no-cache-dir .
RUN chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
