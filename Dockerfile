FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data/media && chown -R app:app /data /app
COPY . .
RUN pip install --upgrade pip && pip install .
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
