FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Use Railway's $PORT environment variable, default to 8080 if not set
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}