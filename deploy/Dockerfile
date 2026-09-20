# Pin both builder/runtime inputs so the same source and lock file do not drift
# onto a different base layer. This digest is for the linux/amd64 image used by
# the current CI and local Docker profile; update it deliberately when upgrading
# the Python patch release or adding another target architecture.
ARG PYTHON_IMAGE=python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ARG AFTERCARE_VERSION=0.1.0a0
ARG AFTERCARE_REVISION=unknown
ARG EGM_GIT_SHA=5d1302e3eb799764c23d8a8e6872abf8547da4ee
ARG AFTERCARE_MIGRATION_VERSION=23
ARG EGM_SCHEMA_VERSION=1
ARG AFTERCARE_EXTRAS=

FROM ${PYTHON_IMAGE} AS builder

ARG AFTERCARE_VERSION
ARG AFTERCARE_REVISION
ARG EGM_GIT_SHA
ARG AFTERCARE_MIGRATION_VERSION
ARG EGM_SCHEMA_VERSION
ARG AFTERCARE_EXTRAS

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_CACHE_DIR=/var/cache/uv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Keep the resolver itself pinned without downloading it from PyPI during the
# build.  uv delegates Git dependencies to the Git executable, so install that
# one builder-only tool with a bounded retry around transient mirror failures.
COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /uvx /usr/local/bin/
RUN set -eu; \
    attempt=1; \
    while true; do \
        if apt-get -o Acquire::Retries=3 update \
            && apt-get -o Acquire::Retries=3 install --no-install-recommends --fix-missing -y git; \
        then \
            break; \
        fi; \
        if [ "$attempt" -ge 3 ]; then \
            exit 1; \
        fi; \
        rm -rf /var/lib/apt/lists/*; \
        attempt=$((attempt + 1)); \
        sleep $((attempt * 3)); \
    done; \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md MANIFEST.in LICENSE THIRD_PARTY_NOTICES.md ./

# The image consumes the committed lock instead of resolving dependency ranges
# on every build.  Install locked dependencies before copying the source so an
# application-only edit does not redownload the world; neither cache is copied
# into the runtime image.
RUN --mount=type=cache,target=/var/cache/uv \
    if [ -n "$AFTERCARE_EXTRAS" ]; then \
        uv sync --frozen --no-dev --no-install-project --extra "$AFTERCARE_EXTRAS"; \
    else \
        uv sync --frozen --no-dev --no-install-project; \
    fi
COPY aftercare_agent ./aftercare_agent
RUN --mount=type=cache,target=/var/cache/uv \
    if [ -n "$AFTERCARE_EXTRAS" ]; then \
        uv sync --frozen --no-dev --no-editable --reinstall-package aftercare-agent --extra "$AFTERCARE_EXTRAS"; \
    else \
        uv sync --frozen --no-dev --no-editable --reinstall-package aftercare-agent; \
    fi


FROM ${PYTHON_IMAGE} AS runtime

ARG AFTERCARE_VERSION
ARG AFTERCARE_REVISION
ARG EGM_GIT_SHA
ARG AFTERCARE_MIGRATION_VERSION
ARG EGM_SCHEMA_VERSION
ARG AFTERCARE_EXTRAS

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv

LABEL org.opencontainers.image.title="Aftercare Agent" \
      org.opencontainers.image.description="Evidence-gated after-sales agent reference implementation" \
      org.opencontainers.image.source="https://github.com/yushui2022/Aftercare-Agent" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$AFTERCARE_VERSION" \
      org.opencontainers.image.revision="$AFTERCARE_REVISION" \
      io.aftercare.egm.git-sha="$EGM_GIT_SHA" \
      io.aftercare.schema.aftercare-migration="$AFTERCARE_MIGRATION_VERSION" \
      io.aftercare.schema.egm="$EGM_SCHEMA_VERSION" \
      io.aftercare.build.extras="$AFTERCARE_EXTRAS"

# The official slim image already carries CA roots plus adduser/passwd tools.
# Avoiding apt here keeps the final stage independent of a package mirror.
RUN groupadd --system --gid 10001 aftercare \
    && useradd --system --uid 10001 --gid aftercare --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin aftercare

# The runtime only executes the locked virtualenv; it never installs packages.
# Remove the base image's pip bundle so its vendored package copies cannot be
# mistaken for runtime dependencies by image scanners and cannot be used as an
# unexpected package-install path in the final container.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip \
           /usr/local/lib/python3.13/site-packages/pip-*.dist-info

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

USER 10001:10001
EXPOSE 8000

CMD ["aftercare-api"]
