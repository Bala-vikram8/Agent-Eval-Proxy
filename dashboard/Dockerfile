FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Build/install tooling is unnecessary in the immutable runtime image.
RUN python -m pip uninstall --yes wheel setuptools && python -m pip uninstall --yes pip
COPY --chown=10001:10001 app.py auth.py monitoring.py monitor.py .
COPY --chown=10001:10001 .streamlit .streamlit
RUN useradd --create-home --uid 10001 app
USER app
VOLUME /tmp
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
