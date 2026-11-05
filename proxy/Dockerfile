FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py store.py streaming.py redaction.py ./
RUN useradd --create-home --uid 10001 app && mkdir -p /var/lib/agenteval && chown app /var/lib/agenteval
USER app
# The trace spool must outlive the container, so compose mounts a volume here.
VOLUME /var/lib/agenteval
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
