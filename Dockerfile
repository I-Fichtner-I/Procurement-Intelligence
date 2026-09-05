# tender-ai als Container - fuer Scheduler-Betrieb (cron, Kubernetes CronJob).
#
# Zwei Stufen: die erste baut ein Wheel, die zweite enthaelt nur Laufzeit und
# Abhaengigkeiten. Installiert wird aus der Lockdatei (requirements.txt), damit
# ein Build heute dieselben Versionen zieht wie in der CI.
FROM python:3.12-slim AS build

WORKDIR /src
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY tender_ai ./tender_ai
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TENDER_AI_DATA_DIR=/app/data

WORKDIR /app

# Erst die gepinnten Abhaengigkeiten (aendern sich selten, bleiben im Cache),
# dann das Paket selbst ohne erneute Aufloesung.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl && rm /tmp/*.whl

COPY config.yaml ./
COPY data/fixtures ./data/fixtures

# Nicht als root laufen: der Container braucht nur sein Datenverzeichnis.
RUN useradd --create-home --uid 10001 tender \
    && mkdir -p /app/data \
    && chown -R tender:tender /app
USER tender

ENTRYPOINT ["tender-ai"]
CMD ["--help"]
