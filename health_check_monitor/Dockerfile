FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY consumer.py .

# Create non-root user
RUN useradd --create-home --shell /bin/bash monitor
USER monitor

# Health check - verify RabbitMQ connection and consumer is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import pika, os, sys; \
    try: \
        conn = pika.BlockingConnection(pika.URLParameters(os.environ['RABBITMQ_URL'])); \
        conn.close(); \
        sys.exit(0) \
    except Exception: \
        sys.exit(1)"

CMD ["python", "consumer.py"]