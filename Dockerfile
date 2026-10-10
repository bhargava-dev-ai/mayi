FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml LICENSE NOTICE README.md ./
COPY mayi ./mayi
RUN pip install --no-cache-dir . && useradd -m mayi && mkdir /data && chown mayi /data
USER mayi
WORKDIR /data
# Put catalog.yaml and tools.json in /data (mount a volume, or build on this image).
# Set MAYI_AGENT_KEY and MAYI_APP_KEY. PORT is honoured (Railway, Render, Fly).
ENV MAYI_HOST=0.0.0.0
EXPOSE 8787
VOLUME /data
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8787'))"
CMD ["sh", "-c", "mayi serve --host ${MAYI_HOST} --port ${PORT:-8787}"]
