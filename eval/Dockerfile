FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir psycopg2-binary openai
COPY eval_engine.py .
CMD ["python", "eval_engine.py"]