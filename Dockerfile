FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    unrar-free \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Keep all app data in the /data volume from the first start (a bind-mounted
# bootstrap.json still overrides this).
RUN echo '{"data_path": "/data"}' > bootstrap.json && chmod 666 bootstrap.json

EXPOSE 8000

CMD ["python", "main.py"]