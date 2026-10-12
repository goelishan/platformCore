# syntax=docker/dockerfile:1
#--------------------------------------------------------------------------------------------------------
# ON-CALL AGENT IMAGE
#--------------------------------------------------------------------------------------------------------
#
#   - one image, three entry points: the agent loop, the triage UI, the journal exporter
#
#   - dependencies from requirements.lock, installed with --require-hashes
#
#   - non-root, no shell login, runs with a read-only root filesystem
#
#   - the RDS CA bundle baked in, because ONCALL_ENV=prod refuses to start without it
#
# Build context is oncall/, so the subproject stays liftable:
#
#   docker build -t oncall:dev oncall/
#



# Tag and manifest-list digest move together: the digest is what is pulled, the tag is
# what a person reads. Resolve with: docker buildx imagetools inspect python:3.11-slim-bookworm
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89



#--------------------------------------------------------------------------------------------------------
# BUILD
#--------------------------------------------------------------------------------------------------------



# hadolint cannot see the ARG's tag and digest, so its untagged-image warning is wrong here.
# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE} AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.lock

# RDS global CA bundle, for sslmode=verify-full against the store.
ADD https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /opt/certs/rds-global-bundle.pem

# COPY keeps the builder's file modes. A checkout with a 0600 umask would leave the source
# unreadable by the runtime user, so modes are normalised here: read for all, execute on
# directories only.
COPY . /opt/src/oncall
RUN chmod -R a+rX,go-w /opt/src /opt/venv \
 && chmod 0444 /opt/certs/rds-global-bundle.pem



#--------------------------------------------------------------------------------------------------------
# RUNTIME
#--------------------------------------------------------------------------------------------------------



# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE}

RUN groupadd --gid 10001 oncall \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin oncall

COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/certs/rds-global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem
COPY --from=build /opt/src/oncall /app/oncall

# HOME points at /tmp, the one writable path besides the data volume: Streamlit and
# botocore both write small caches under HOME. The data directory is a subdirectory of the
# volume, not its root: the agent sets it to 0700, and only a directory's owner may chmod
# it, while a volume's root belongs to whoever provisioned it.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    ONCALL_DATA_DIR=/var/lib/oncall/data \
    ONCALL_STORE_SSLROOTCERT=/etc/ssl/certs/rds-global-bundle.pem

USER 10001:10001
WORKDIR /app

ENTRYPOINT ["python", "-m"]
CMD ["oncall.agent"]
