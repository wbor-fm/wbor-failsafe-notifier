# Health Check Monitor

Container to monitor receipt of health check messages (via RabbitMQ), sending Discord alerts checks are missed.

## Features

- Dockerized for easy deployment
- RabbitMQ queue integration
- Configurable timeout
- Emits Discord webhook alerts when timeouts occur

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Required environment variables:

- `DISCORD_WEBHOOK_URL`: Discord webhook URL for alerts
- `RABBITMQ_URL`: RabbitMQ connection string

Optional configuration variables:

- `HEALTH_CHECK_QUEUE`: Queue name for health check messages (default: `health_checks`)
- `RABBITMQ_EXCHANGE_NAME`: Exchange name for health check messages (default: `healthcheck`)  
- `RABBITMQ_HEALTHCHECK_ROUTING_KEY`: Routing key for health check messages (default: `health.failsafe-status`)
- `CHECK_INTERVAL_SECONDS`: How often to check for timeouts (default: `300`, i.e., 5 minutes)
- `TIMEOUT_THRESHOLD_SECONDS`: Timeout threshold before alerting (default: `600`, i.e., 10 minutes)

**RabbitMQ Message Routing:**
The consumer automatically binds the health check queue to the specified exchange with the routing key:

- **`healthcheck` exchange** + `health.failsafe-status` key → Health check monitoring messages
- **`notifications` exchange** + `notification.failsafe-status` key → Failsafe status change alerts  
- **`commands` exchange** + `command.failsafe-override` key → Failsafe override commands

## Usage

```bash
# Quick rebuild and run (stops existing container, builds, runs, follows logs)
make

# Build the image only
make build

# Run the container (creates logs directory, uses .env file)
make run

# Follow container logs
make logsf

# Check runnning container's health status
make health

# Execute shell in the running container
make exec

# Stop and remove container
make stop

# Clean up (stop container and remove image)
make clean
```

**Environment Configuration:**

- The Makefile uses `.env` file for configuration
- Supports both Docker and Podman via `DOCKER_TOOL`

## Health Check Message Format

The consumer expects JSON messages following this format:

```json
{
  "timestamp": "2025-01-24T10:30:00Z",
  "service": "your-service-name",
  "status": "healthy"
}
```

## Monitoring

- Container logs show health check activity and alerts
- RabbitMQ Management UI: <http://localhost:15672> (credentials `guest`/`guest` unless changed)
