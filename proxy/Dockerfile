FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Build/install tooling is unnecessary in the immutable runtime image.
RUN python -m pip uninstall --yes wheel setuptools && python -m pip uninstall --yes pip
COPY --chown=10001:10001 main.py store.py streaming.py security.py privacy.py queueing.py ingest_worker.py ./
RUN useradd --create-home --uid 10001 app && mkdir -p /var/lib/agenteval && chown app:app /var/lib/agenteval
USER app
VOLUME /tmp
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
