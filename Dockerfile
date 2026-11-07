FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_DOTENV_DISABLED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ideyafix \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ideyafix \
    && install -d -m 0700 -o 10001 -g 10001 /app/runtime \
    && install -d -m 1777 /private/tmp

COPY requirements.txt requirements.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt \
    && python -m pip check

COPY app ./app
COPY platform_engine ./platform_engine
COPY tests ./tests
COPY scripts/container-health.py ./scripts/container-health.py
COPY config/max.example.json ./config/max.example.json
COPY docs/DOCKER.md ./docs/DOCKER.md
COPY README.md run.py ./

USER 10001:10001
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-B", "scripts/container-health.py", "--config", "/run/private/connection.json"]
ENTRYPOINT ["python", "-B", "-m", "app.polling"]
CMD ["--config", "/run/private/connection.json"]
