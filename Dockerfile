FROM python:3.12-alpine

WORKDIR /app
COPY core.py pool.py server.py ui.html login.py ./
RUN mkdir -p /app/data && adduser -D -u 10001 today && chown -R today:today /app

USER today
ENV LISTEN_HOST=0.0.0.0 LISTEN_PORT=8770 PYTHONUNBUFFERED=1
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8770/health',timeout=4)"

CMD ["python3", "server.py"]
