FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir streamlit psycopg2-binary pandas

COPY app.py .

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
