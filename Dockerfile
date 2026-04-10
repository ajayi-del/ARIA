FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure persistent log directory exists
RUN mkdir -p /data/logs && ln -sfn /data/logs /app/logs

EXPOSE 8080
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "main.py"]
