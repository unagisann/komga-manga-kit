FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MANGA_KIT_DATA_ROOT=/state
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home manga && mkdir /state && chown manga:manga /state
USER manga
EXPOSE 5080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5080/healthz', timeout=2)"
CMD ["gunicorn", "--bind", "0.0.0.0:5080", "--workers", "1", "--threads", "4", "--access-logfile", "-", "manga_kit:create_app()"]
