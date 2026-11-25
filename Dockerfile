# 离线镜像：从 vendor/ 内的已验证 wheel 安装，构建时不访问网络。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY vendor/ /srv/vendor/
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-index --find-links=/srv/vendor -r /srv/requirements.txt \
    && rm -rf /srv/vendor /srv/requirements.txt

COPY app/ /srv/app/
COPY templates/ /srv/templates/
COPY static/ /srv/static/
COPY wsgi.py /srv/wsgi.py

RUN useradd --system --uid 10001 --home /srv handover \
    && mkdir -p /srv/data \
    && chown -R handover:handover /srv/data
USER handover

ENV DATA_DIR=/srv/data \
    PORT=8000 \
    HOST=0.0.0.0 \
    THREADS=8

EXPOSE 8000
VOLUME ["/srv/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "wsgi.py"]
