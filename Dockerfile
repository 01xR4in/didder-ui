# syntax=docker/dockerfile:1

##############################################################################
# Stage 1 — build didder from source.
#
# v1.3.0 (2022-12-20) is the latest tagged release, but the `mmcq:N` automatic
# palette flag only exists on main. DIDDER_REF therefore pins an exact commit
# for reproducibility rather than tracking @latest. To build the release
# instead:  docker compose build --build-arg DIDDER_REF=v1.3.0
# The app detects mmcq support at runtime and disables the control if absent.
##############################################################################
FROM golang:alpine AS didder-build

ARG DIDDER_REF=408a18aef878b456fe5cdbec406070fd5bd5c2d2
ARG DIDDER_VERSION=v1.3.0+mmcq

RUN apk add --no-cache git

WORKDIR /src
RUN git clone --no-checkout https://github.com/makeworld-the-better-one/didder.git . \
 && git checkout --detach "${DIDDER_REF}"

ENV CGO_ENABLED=0
RUN go build -trimpath \
      -ldflags "-s -w -X main.version=${DIDDER_VERSION} -X main.commit=${DIDDER_REF} -X main.builtBy=docker" \
      -o /out/didder . \
 && /out/didder --version

##############################################################################
# Stage 2 — runtime. No Go toolchain, just the static didder binary.
##############################################################################
FROM python:3.12-slim AS runtime

# Match the host owner of ./work so the bind mount is writable. Override with
# APP_UID/APP_GID (see docker-compose.yml) if your uid is not 1000.
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DIDDER_WORK_DIR=/work

COPY --from=didder-build /out/didder /usr/local/bin/didder

RUN groupadd --gid "${APP_GID}" dither \
 && useradd --uid "${APP_UID}" --gid dither --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin dither \
 && mkdir -p /work \
 && chown dither:dither /work \
 && didder --version

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

USER dither:dither
EXPOSE 8000

# Binds 0.0.0.0 *inside* the container only; compose publishes it on
# 127.0.0.1 of the host.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
