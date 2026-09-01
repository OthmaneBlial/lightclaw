FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/lightclaw

RUN groupadd --system lightclaw \
    && useradd --system --gid lightclaw --create-home --home-dir /home/lightclaw lightclaw

WORKDIR /opt/lightclaw
COPY . .
RUN python -m pip install --no-cache-dir '.[providers]' \
    && rm -rf /root/.cache/pip /opt/lightclaw/.git /opt/lightclaw/build /opt/lightclaw/dist

USER lightclaw:lightclaw
WORKDIR /home/lightclaw
VOLUME ["/home/lightclaw/.config/lightclaw", "/home/lightclaw/.lightclaw"]

ENTRYPOINT ["lightclaw"]
CMD ["--help"]
