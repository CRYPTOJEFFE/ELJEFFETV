FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY romcatalog /app/romcatalog

# Docker initializes the named volume from this owned, empty directory.
# ROMs and reports are separate bind mounts supplied at run time.
RUN mkdir -p /state/catalog /state/cache \
    && chown -R "${APP_UID}:${APP_GID}" /state

USER ${APP_UID}:${APP_GID}
ENTRYPOINT ["python", "-m", "romcatalog"]
CMD ["--help"]
