FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/lightclaw
COPY . .
RUN python -m venv /opt/lightclaw/.venv \
    && /opt/lightclaw/.venv/bin/python -m pip install --require-hashes -r requirements-pip.txt \
    && /opt/lightclaw/.venv/bin/python -m pip install --require-hashes -r requirements-runtime.txt \
    && /opt/lightclaw/.venv/bin/python -m pip install --no-deps --no-build-isolation . \
    && /opt/lightclaw/.venv/bin/python -m pip uninstall -y setuptools wheel packaging \
    && /opt/lightclaw/.venv/bin/python -m pip check \
    && /opt/lightclaw/.venv/bin/python -m pip uninstall -y pip

FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/lightclaw \
    PATH=/opt/lightclaw/.venv/bin:$PATH

RUN groupadd --system lightclaw \
    && useradd --system --gid lightclaw --create-home --home-dir /home/lightclaw lightclaw

COPY --from=build --chown=lightclaw:lightclaw /opt/lightclaw/.venv /opt/lightclaw/.venv

USER lightclaw:lightclaw
WORKDIR /home/lightclaw
VOLUME ["/home/lightclaw/.config/lightclaw", "/home/lightclaw/.lightclaw"]

ENTRYPOINT ["lightclaw"]
CMD ["--help"]
