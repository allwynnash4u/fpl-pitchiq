FROM python:3.12-slim

WORKDIR /app

COPY fpl_engine /app/fpl_engine
COPY README.md /app/README.md
COPY data /app/data

ENV PYTHONUNBUFFERED=1
ENV FPL_HOST=0.0.0.0
ENV FPL_DATA_DIR=/app/data

RUN mkdir -p /app/data/cache

CMD ["sh", "-c", "if [ ! -f /app/data/fpl.sqlite3 ]; then echo 'No SQLite database found; building from cached FPL data'; python -m fpl_engine update; fi; python -m fpl_engine serve --host 0.0.0.0 --port ${PORT:-8765} --no-initial-update"]
