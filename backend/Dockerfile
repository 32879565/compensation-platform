FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

# Install the locked third-party runtime before copying application code so a
# source-only change does not re-resolve or reinstall dependencies.
COPY pyproject.toml requirements-build.lock requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.lock \
    && pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker-entrypoint.sh /usr/local/bin/compensation-entrypoint
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && pip check

# 非特权用户运行（承载薪资数据的服务默认加固）
RUN chmod 755 /usr/local/bin/compensation-entrypoint \
    && useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# Only the fixed frontend proxy address in deploy/docker-compose.yml may supply
# forwarded headers. Direct requests to the loopback API port therefore cannot
# forge the login-throttle or audit IP through X-Forwarded-For.
ENTRYPOINT ["/usr/local/bin/compensation-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "172.30.0.10"]
