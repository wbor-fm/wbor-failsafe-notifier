# Health Check Monitor

A Docker container that monitors RabbitMQ health check messages and sends Discord alerts when health checks are missed.

## Features

- Consumes health check messages from RabbitMQ queue
- Monitors for missing health checks with configurable timeout
- Sends Discord webhook alerts when timeouts occur
- Dockerized for easy deployment
- Includes RabbitMQ instance in docker-compose setup

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Required environment variables:

- `DISCORD_WEBHOOK_URL`: Discord webhook URL for alerts
- `RABBITMQ_URL`: RabbitMQ connection string
- `HEALTH_CHECK_QUEUE`: Queue name for health check messages
- `CHECK_INTERVAL_SECONDS`: How often to check for timeouts (default: `300`)
- `TIMEOUT_THRESHOLD_SECONDS`: Timeout threshold before alerting (default: `600`)

## Usage

### Using Docker Compose (Recommended)

```bash
# Start the services
docker-compose up -d

# View logs
docker-compose logs -f health-check-monitor

# Stop the services
docker-compose down
```

### Using Docker directly

```bash
# Build the image
docker build -t health-check-monitor .

# Run the container
docker run -d \
  --name health-check-monitor \
  -e DISCORD_WEBHOOK_URL="your_webhook_url" \
  -e RABBITMQ_URL="amqp://guest:guest@localhost:5672/" \
  health-check-monitor
```

### Using with External RabbitMQ Container

If you already have a RabbitMQ container running, you can use the health check monitor standalone:

1. **Remove RabbitMQ from docker-compose.yml** or create a new compose file:

    ```yaml
    services:
      health-check-monitor:
        build: .
        restart: unless-stopped
        environment:
          - RABBITMQ_URL=amqp://guest:guest@your-rabbitmq-host:5672/
          - HEALTH_CHECK_QUEUE=health_checks
          - DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL}
          - CHECK_INTERVAL_SECONDS=300
          - TIMEOUT_THRESHOLD_SECONDS=600
        networks:
          - your-existing-network

    networks:
      your-existing-network:
        external: true
    ```

2. **Update the RABBITMQ_URL** in your `.env` file to point to your existing RabbitMQ instance:

    ```bash
    # Point to existing RabbitMQ container
    RABBITMQ_URL=amqp://username:password@rabbitmq-container-name:5672/

    # Or point to external RabbitMQ server
    RABBITMQ_URL=amqp://username:password@rabbitmq.example.com:5672/
    ```

3. **Ensure the health check queue exists** in your RabbitMQ instance, or the consumer will create it automatically when it starts.

4. **Connect to the same Docker network** if your RabbitMQ container is in a custom network:

    ```bash
    # Add your monitor to the existing network
    docker network connect your-rabbitmq-network health-check-monitor
    ```

## Health Check Message Format

The consumer expects JSON messages in the following format:

```json
{
  "timestamp": "2025-01-24T10:30:00Z",
  "service": "your-service-name",
  "status": "healthy"
}
```

## Monitoring

- RabbitMQ Management UI: <http://localhost:15672> (credentials guest/guest unless changed)
- Container logs show health check activity and alerts
