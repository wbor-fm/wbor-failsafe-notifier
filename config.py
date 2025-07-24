"""Configuration module for WBOR Failsafe Notifier.

This module centralizes all configuration settings including environment variables,
RabbitMQ exchange definitions, and application constants.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import dotenv_values
import pytz


@dataclass
class RabbitMQExchange:
    """Configuration for a RabbitMQ exchange."""

    name: str
    routing_keys: dict[str, str]


@dataclass
class Config:
    """Main configuration class for the WBOR Failsafe Notifier."""

    # Hardware Configuration
    pin_assignment: str
    backup_input: str
    primary_source: str
    backup_source: str
    timezone: str
    dry_run: bool

    # Discord Configuration
    discord_webhook_url: str | None
    author_name: str | None
    author_url: str | None
    author_icon_url: str | None

    # Spinitron Configuration
    spinitron_api_base_url: str | None

    # GroupMe Configuration
    groupme_api_base_url: str
    groupme_bot_id_mgmt: str | None
    groupme_bot_id_djs: str | None

    # Email Configuration
    smtp_server: str | None
    smtp_port: str | None
    smtp_username: str | None
    smtp_password: str | None
    from_email: str | None
    error_email: str | None

    # RabbitMQ Configuration
    rabbitmq_amqp_url: str | None

    # RabbitMQ Exchanges
    notifications_exchange: RabbitMQExchange
    healthcheck_exchange: RabbitMQExchange
    commands_exchange: RabbitMQExchange

    # Additional RabbitMQ Settings
    rabbitmq_override_queue: str

    # Timezone object
    configured_timezone: pytz.BaseTzInfo


def load_config() -> Config:
    """Load configuration from environment variables and .env file.

    Returns:
        Config: The loaded configuration object.

    Raises:
        ValueError: If required configuration values are missing or invalid.
    """
    # Load configuration first to check for dry run mode
    config = dotenv_values(".env")
    if not config:
        config = dict(os.environ)

    # Get timezone early
    timezone_name = config.get("TIMEZONE") or "America/New_York"

    # Check for dry run mode (used in CI/testing)
    dry_run = (config.get("DRY_RUN") or "").lower() in ("true", "1", "yes")

    # Validate required configurations
    required_configs = [
        "PIN_ASSIGNMENT",
        "BACKUP_INPUT",
    ]
    missing_configs = [key for key in required_configs if not config.get(key)]
    if missing_configs:
        missing_list = "`, `".join(missing_configs)
        cfg_err_msg = (
            f"Required configuration(s) `{missing_list}` must be set in "
            ".env file or environment!"
        )
        raise ValueError(cfg_err_msg)

    pin_assignment = config["PIN_ASSIGNMENT"]
    if not pin_assignment:
        msg = "PIN_ASSIGNMENT cannot be empty"
        raise ValueError(msg)
    backup_input = str(config.get("BACKUP_INPUT", "B")).upper()

    # Determine primary and backup sources
    backup_source = backup_input
    primary_source = "B" if backup_source == "A" else "A"
    if backup_source not in ["A", "B"]:
        msg = "`BACKUP_INPUT` must be either 'A' or 'B'."
        raise ValueError(msg)

    # Validate timezone and create timezone object
    try:
        configured_timezone = pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        configured_timezone = pytz.timezone("America/New_York")
        timezone_name = "America/New_York"

    # Define RabbitMQ exchanges with their routing keys
    notifications_exchange = RabbitMQExchange(
        name=config.get("RABBITMQ_NOTIFICATIONS_EXCHANGE") or "notifications",
        routing_keys={
            "source_change": config.get("RABBITMQ_NOTIFICATIONS_ROUTING_KEY")
            or "notification.failsafe-status",
        },
    )

    healthcheck_exchange = RabbitMQExchange(
        name=config.get("RABBITMQ_HEALTHCHECK_EXCHANGE") or "healthcheck",
        routing_keys={
            "health_ping": config.get("RABBITMQ_HEALTHCHECK_ROUTING_KEY")
            or "health.failsafe-status",
        },
    )

    commands_exchange = RabbitMQExchange(
        name=config.get("RABBITMQ_COMMANDS_EXCHANGE") or "commands",
        routing_keys={
            "override": config.get("RABBITMQ_COMMANDS_OVERRIDE_ROUTING_KEY")
            or "command.failsafe-override",
        },
    )

    return Config(
        # Hardware Configuration
        pin_assignment=pin_assignment,
        backup_input=backup_input,
        primary_source=primary_source,
        backup_source=backup_source,
        timezone=timezone_name,
        dry_run=dry_run,
        # Discord Configuration
        discord_webhook_url=config.get("DISCORD_WEBHOOK_URL"),
        author_name=config.get("AUTHOR_NAME") or "wbor-failsafe-notifier",
        author_url=config.get("AUTHOR_URL")
        or "https://github.com/WBOR-91-1-FM/wbor-failsafe-notifier",
        author_icon_url=config.get("AUTHOR_ICON_URL"),
        # Spinitron Configuration
        spinitron_api_base_url=config.get("SPINITRON_API_BASE_URL"),
        # GroupMe Configuration
        groupme_api_base_url=config.get("GROUPME_API_BASE_URL")
        or "https://api.groupme.com/v3",
        groupme_bot_id_mgmt=config.get("GROUPME_BOT_ID_MGMT"),
        groupme_bot_id_djs=config.get("GROUPME_BOT_ID_DJS"),
        # Email Configuration
        smtp_server=config.get("SMTP_SERVER"),
        smtp_port=config.get("SMTP_PORT"),
        smtp_username=config.get("SMTP_USERNAME"),
        smtp_password=config.get("SMTP_PASSWORD"),
        from_email=config.get("FROM_EMAIL"),
        error_email=config.get("ERROR_EMAIL"),
        # RabbitMQ Configuration
        rabbitmq_amqp_url=config.get("RABBITMQ_AMQP_URL"),
        # RabbitMQ Exchanges
        notifications_exchange=notifications_exchange,
        healthcheck_exchange=healthcheck_exchange,
        commands_exchange=commands_exchange,
        # Additional RabbitMQ Settings
        rabbitmq_override_queue=config.get("RABBITMQ_OVERRIDE_QUEUE") or "commands",
        # Timezone object
        configured_timezone=configured_timezone,
    )


# Discord embed colors (in decimal)
DISCORD_EMBED_ERROR_COLOR = 16711680  # Red
DISCORD_EMBED_WARNING_COLOR = 16776960  # Yellow
DISCORD_EMBED_SUCCESS_COLOR = 65280  # Green
