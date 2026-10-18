ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MANGA_KIT_DATA_ROOT=/state
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libarchive13t64 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md requirements.lock ./
COPY licenses ./licenses
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home manga && mkdir /state && chown manga:manga /state
USER manga
EXPOSE 5080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5080/healthz', timeout=2)"
CMD ["gunicorn", "--bind", "0.0.0.0:5080", "--workers", "1", "--threads", "4", "--access-logfile", "-", "manga_kit:create_app()"]
