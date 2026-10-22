FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY eval_engine.py .
RUN useradd --create-home --uid 10001 app
USER app
CMD ["python", "eval_engine.py"]
