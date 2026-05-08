ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip jq ffmpeg

COPY app.py /app/app.py
COPY templates /app/templates
COPY static /app/static
COPY requirements.txt /app/requirements.txt
COPY run.sh /run.sh

RUN pip3 install --break-system-packages --no-cache-dir -r /app/requirements.txt && \
    chmod a+x /run.sh

WORKDIR /app
CMD ["/run.sh"]
