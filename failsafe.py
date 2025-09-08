"""Monitors input on a microcontroller board and sends a Discord webhook notification.

This module distinguishes between primary and backup sources based on pin state.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
import logging
import smtplib
import time
from typing import Any

from dotenv import dotenv_values
import requests

from config import (
    DISCORD_EMBED_ERROR_COLOR,
    DISCORD_EMBED_SUCCESS_COLOR,
    DISCORD_EMBED_WARNING_COLOR,
    load_config,
)
from utils.logging import configure_logging
from utils.rabbitmq_consumer import RabbitMQConsumer
from utils.rabbitmq_publisher import RabbitMQPublisher

# Load configuration
app_config = load_config()

# Hardware imports - skip in dry run mode to avoid hardware dependencies
if not app_config.dry_run:
    import board
    import digitalio
else:
    # Mock board and digitalio for dry run
    class MockBoard:
        """Mock board class for dry run mode."""

        def __getattr__(self, name: str) -> str:
            """Return mock pin name for any attribute access."""
            return f"mock_pin_{name}"

    class MockDigitalIO:
        """Mock digitalio module for dry run mode."""

        class DigitalInOut:
            """Mock digital input/output pin for dry run mode."""

            def __init__(self, _pin: str) -> None:
                """Initialize mock pin with default state."""
                self.value = False

            def switch_to_input(self) -> None:
                """Mock method to switch pin to input mode."""

    board = MockBoard()
    digitalio = MockDigitalIO()

logging.root.handlers = []
logger = configure_logging()

# Log warning message if .env file wasn't found
if not dotenv_values(".env"):
    logger.warning(
        ".env file is empty or not found. Attempting to use system environment "
        "variables."
    )


PIN_NAME = app_config.pin_assignment
PRIMARY_SOURCE = app_config.primary_source
BACKUP_SOURCE = app_config.backup_source
DRY_RUN = app_config.dry_run

# Initialize hardware pin (skip in dry run mode)
if not DRY_RUN:
    try:
        pin = getattr(board, PIN_NAME)
    except AttributeError as exc:
        logger.critical("%s is not a valid pin name for this board.", PIN_NAME)
        msg = f"{PIN_NAME} is not a valid pin name for this board."
        raise ValueError(msg) from exc
    except (
        Exception
    ) as e:  # Catch other board related errors, e.g. if Blinka is not setup
        logger.critical(
            "Failed to access board attribute for pin %s: %s",
            PIN_NAME,
            e,
            exc_info=True,
        )
        msg = f"Board or pin initialization error for {PIN_NAME}"
        raise RuntimeError(msg) from e

    try:
        DIGITAL_PIN = digitalio.DigitalInOut(pin)
        DIGITAL_PIN.switch_to_input()
    except Exception as e:
        logger.critical("Failed to initialize digital pin %s: %s", PIN_NAME, e)
        msg = f"Failed to initialize digital pin {PIN_NAME}"
        raise RuntimeError(msg) from e
else:
    logger.info("DRY_RUN mode enabled - skipping hardware initialization")

    # Create a mock pin object for dry run
    class MockPin:
        """Mock pin class for dry run mode."""

        def __init__(self) -> None:
            """Initialize mock pin with default state."""
            self.value = False

    DIGITAL_PIN = MockPin()


# Discord base payload
DISCORD_EMBED_PAYLOAD_BASE = {
    "embeds": [
        {
            "title": "Failsafe Gadget - Source Switched",
            "author": {
                "name": app_config.author_name,
                "url": app_config.author_url,
                "icon_url": app_config.author_icon_url,
            },
        }
    ],
}


class OverrideManager:
    """Manages temporary override state for failsafe notifications.

    Tracks whether failsafe notifications are temporarily disabled and when the override
    period should end.
    """

    def __init__(self) -> None:
        """Initialize the override manager with default state."""
        self.active = False
        self.end_time: datetime | None = None
        self.last_healthcheck_time: datetime | None = None
        self.healthcheck_failures = 0
        self.max_healthcheck_failures = 5
        self.state_changed_during_override = False
        self.pending_source_change: tuple[str, str] | None = None
        self.override_logged = False
        self.original_source_before_override: str | None = None
        self.last_healthcheck_retry_time: datetime | None = None


# Global state manager for temporary override functionality
override_manager = OverrideManager()


def initialize_rabbitmq_publishers() -> dict[str, RabbitMQPublisher | None]:
    """Initializes and returns RabbitMQ publishers for all exchanges.

    Returns:
        Dictionary mapping exchange names to RabbitMQPublisher instances.
    """
    publishers: dict[str, RabbitMQPublisher | None] = {
        "notifications": None,
        "healthcheck": None,
        "commands": None,
    }

    if app_config.rabbitmq_amqp_url:
        for exchange_type, exchange in [
            ("notifications", app_config.notifications_exchange),
            ("healthcheck", app_config.healthcheck_exchange),
            ("commands", app_config.commands_exchange),
        ]:
            try:
                publisher = RabbitMQPublisher(
                    amqp_url=app_config.rabbitmq_amqp_url, exchange_name=exchange.name
                )
                publishers[exchange_type] = publisher
                logger.info(
                    "RabbitMQ publisher initialized for %s exchange `%s`.",
                    exchange_type,
                    exchange.name,
                )
            except Exception:
                logger.exception(
                    "Failed to initialize RabbitMQ publisher for %s exchange. "
                    "Proceeding without.",
                    exchange_type,
                )
    else:
        logger.info(
            "RabbitMQ AMQP URL not configured. RabbitMQ publishing will be disabled."
        )

    return publishers


def initialize_rabbitmq_consumer() -> RabbitMQConsumer | None:
    """Initializes and returns RabbitMQConsumer if configured.

    Returns:
        RabbitMQConsumer instance if RABBITMQ_AMQP_URL is configured, None otherwise.
    """
    if app_config.rabbitmq_amqp_url:
        try:
            consumer = RabbitMQConsumer(
                amqp_url=app_config.rabbitmq_amqp_url,
                queue_name=app_config.rabbitmq_override_queue,
                exchange_name=app_config.commands_exchange.name,
                routing_key=app_config.commands_exchange.routing_keys["override"],
                callback=handle_override_message,
            )
            logger.info(
                "RabbitMQ consumer initialized for queue `%s`.",
                app_config.rabbitmq_override_queue,
            )
        except Exception:
            logger.exception(
                "Failed to initialize RabbitMQ consumer. Proceeding without "
                "override functionality."
            )
        else:
            return consumer
    else:
        logger.info(
            "RabbitMQ AMQP URL not configured. Override functionality will be disabled."
        )
    return None


def handle_override_message(message: dict[str, Any]) -> None:
    """Handle incoming override messages from RabbitMQ.

    Expected message format:
    ```
    {"action": "enable_override", "duration_minutes": 5}
    ```

    If `duration_minutes` is not provided, defaults to 5 minutes.
    """
    try:
        action = message.get("action")
        if action == "enable_override":
            duration_minutes = message.get("duration_minutes", 5)
            override_manager.active = True
            override_manager.end_time = datetime.now(timezone.utc) + timedelta(
                minutes=duration_minutes
            )
            override_manager.state_changed_during_override = False
            override_manager.pending_source_change = None
            override_manager.override_logged = False
            override_manager.original_source_before_override = None

            logger.info(
                "Temporary override activated for %d minutes (until %s UTC)",
                duration_minutes,
                override_manager.end_time.isoformat(),
            )

        elif action == "disable_override":
            override_manager.active = False
            override_manager.end_time = None
            override_manager.state_changed_during_override = False
            override_manager.pending_source_change = None
            override_manager.override_logged = False
            override_manager.original_source_before_override = None
            logger.info("Temporary override manually disabled")

        else:
            logger.warning("Unknown override action received: %s", action)

    except Exception:
        logger.exception("Error processing override message")


def check_override_expiry() -> bool:
    """Check if the temporary override has expired and disable it if so.

    Compares current time with override end time and resets override state if the period
    has elapsed.

    Returns:
        True if override just expired, False otherwise
    """
    if override_manager.active and override_manager.end_time:
        current_time = datetime.now(timezone.utc)
        if current_time >= override_manager.end_time:
            override_manager.active = False
            override_manager.end_time = None
            override_manager.override_logged = False
            logger.info(
                "Temporary override expired/disabled: returning to normal operation"
            )
            return True
    return False


def send_health_check(publishers: dict[str, RabbitMQPublisher | None]) -> None:
    """Send a health check message to RabbitMQ indicating the system is alive.

    Publishes a heartbeat message with current system status including pin state, active
    source, and override status. After max failures, retries every hour to allow
    recovery if RabbitMQ comes back online.
    """
    healthcheck_publisher = publishers.get("healthcheck")
    if not healthcheck_publisher:
        return

    current_time = datetime.now(timezone.utc)

    # If we've exceeded max failures, only retry every hour
    if (
        override_manager.healthcheck_failures
        >= override_manager.max_healthcheck_failures
    ):
        # Check if it's time for an hourly retry
        if (
            override_manager.last_healthcheck_retry_time is None
            or current_time - override_manager.last_healthcheck_retry_time
            >= timedelta(hours=1)
        ):
            logger.info(
                "Attempting hourly health check retry after %d failures",
                override_manager.healthcheck_failures,
            )
            override_manager.last_healthcheck_retry_time = current_time
            # Continue to attempt sending below
        else:
            return  # Skip if not time for retry yet

    # Send health check every hour (or during retry attempts)
    if (
        override_manager.last_healthcheck_time is None
        or current_time - override_manager.last_healthcheck_time >= timedelta(hours=1)
    ):
        health_payload = {
            "source_application": "wbor-failsafe-notifier",
            "event_type": "health_check",
            "timestamp_utc": current_time.isoformat(),
            "status": "alive",
            "pin_name": PIN_NAME,
            "current_pin_state": DIGITAL_PIN.value,
            "active_source": PRIMARY_SOURCE if DIGITAL_PIN.value else BACKUP_SOURCE,
            "override_active": override_manager.active,
            "override_end_time": override_manager.end_time.isoformat()
            if override_manager.end_time
            else None,
        }

        if healthcheck_publisher.publish(
            app_config.healthcheck_exchange.routing_keys["health_ping"], health_payload
        ):
            override_manager.last_healthcheck_time = current_time

            # If this was a successful retry after failures, log recovery
            if (
                override_manager.healthcheck_failures
                >= override_manager.max_healthcheck_failures
            ):
                logger.info(
                    "Health check publishing recovered after %d failures. "
                    "RabbitMQ connection restored.",
                    override_manager.healthcheck_failures,
                )

            override_manager.healthcheck_failures = 0  # Reset on success
            logger.info("Health check message sent successfully")
        else:
            # Only increment failures if we haven't reached max yet
            # (to avoid inflating the counter during hourly retries)
            if (
                override_manager.healthcheck_failures
                < override_manager.max_healthcheck_failures
            ):
                override_manager.healthcheck_failures += 1

            logger.error(
                "Failed to send health check message (attempt %d/%d)",
                override_manager.healthcheck_failures,
                override_manager.max_healthcheck_failures,
            )

            if (
                override_manager.healthcheck_failures
                >= override_manager.max_healthcheck_failures
            ):
                logger.warning(
                    "Maximum health check failures reached (%d). Will retry every hour "
                    "until RabbitMQ connection is restored.",
                    override_manager.max_healthcheck_failures,
                )


def api_get(endpoint: str) -> dict | None:
    """Make a GET request to the Spinitron API and return the JSON response.

    Args:
        endpoint: The API endpoint to fetch (without base URL).

    Returns:
        The JSON response from the API, or None if an error occurred.
    """
    if not app_config.spinitron_api_base_url:
        logger.warning(
            "SPINITRON_API_BASE_URL not configured. Cannot make API GET request."
        )
        return None
    url = f"{app_config.spinitron_api_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]
    except requests.exceptions.HTTPError as e:
        logger.exception(
            "HTTP error fetching `%s`: Status %s, Response: %s",
            url,
            e.response.status_code,
            e.response.text,
        )
    except requests.exceptions.RequestException:
        logger.exception("Request error fetching `%s`", url)
    except Exception:
        logger.exception("Unexpected error fetching `%s`", url)
    return None


def send_email(subject: str, body: str, to_email: str) -> None:
    """Send an email using the configured SMTP server.

    Parameters:
    - subject (str): The subject of the email.
    - body (str): The body of the email.
    - to_email (str): The recipient's email address.
    """
    logger.info("Attempting to send email to `%s` with subject: %s", to_email, subject)

    if not app_config.smtp_server:
        logger.error("SMTP_SERVER is not set, cannot send email.")
        return
    if not app_config.smtp_username:
        logger.error("SMTP_USERNAME is not set, cannot send email.")
        return
    if not app_config.smtp_password:
        logger.error("SMTP_PASSWORD is not set, cannot send email.")
        return
    if not app_config.from_email:
        logger.error("FROM_EMAIL is not set, cannot send email.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["To"] = to_email
        msg["From"] = app_config.from_email

        smtp_port_int = (
            int(app_config.smtp_port) if app_config.smtp_port else 587
        )  # Default to 587 if not set (common for TLS)

        with smtplib.SMTP(app_config.smtp_server, smtp_port_int, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(app_config.smtp_username, app_config.smtp_password)
            server.sendmail(app_config.from_email, [to_email], msg.as_string())
        logger.info("Email successfully sent to `%s`", to_email)
    except smtplib.SMTPRecipientsRefused as e:
        logger.exception("SMTP recipients refused for `%s`: %s", to_email, e.recipients)
        # Avoid self-notification loop
        if app_config.error_email and to_email != app_config.error_email:
            send_email(
                subject="Failsafe Gadget - SMTP Recipients Refused",
                body=f"SMTP recipients refused for {to_email}: {e.recipients}",
                to_email=app_config.error_email,
            )
    except Exception as e:
        logger.exception("Failed to send email to %s", to_email)
        if app_config.error_email and to_email != app_config.error_email:
            send_email(
                subject="Failsafe Gadget - Email Sending Failure",
                body=f"General failure sending email to {to_email}: {e}",
                to_email=app_config.error_email,
            )


def get_current_playlist() -> dict | None:
    """Get the current playlist from Spinitron API.

    Returns:
        The current playlist data if available, None otherwise.
    """
    logger.debug("Fetching current playlist from Spinitron API")
    data = api_get("playlists?count=1")  # Fetch only the latest one
    if data:
        items = data.get("items", [])
        if items:
            playlist = items[0]
            logger.debug("Current playlist: `%s`", playlist.get("title", "N/A"))
            return playlist  # type: ignore[no-any-return]
        logger.warning("No playlist items found in the response: %s", data)
    return None


def get_show(show_id: int) -> dict | None:
    """Get show information from Spinitron API.

    Parameters:
    - show_id (int): The ID of the show to fetch.

    Returns:
    - dict: The show information, or None if an error occurred.
    """
    logger.debug("Fetching show with ID `%s`", show_id)
    return api_get(f"shows/{show_id}")


def get_show_persona_ids(show: dict) -> list[int]:
    """Get persona IDs from a show object.

    Extracts persona IDs from the `_links` field of the show object by parsing
    the href URLs for each persona link.

    Args:
        show: The show object containing persona links in its _links field.

    Returns:
        A list of persona IDs extracted from the show object. Returns empty
        list if no persona links are found or if parsing fails.
    """
    try:
        personas_links = show.get("_links", {}).get("personas", [])
        ids = []
        for p_link in personas_links:
            if p_link.get("href"):
                try:
                    # Extract the last part of the href, assuming it's the ID
                    persona_id_str = p_link["href"].rstrip("/").split("/")[-1]
                    if persona_id_str.isdigit():
                        ids.append(int(persona_id_str))
                except (ValueError, IndexError):
                    logger.warning(
                        "Could not parse persona ID from href: `%s`", p_link["href"]
                    )
    except Exception:
        logger.exception("Error parsing persona IDs from show")
        return []
    else:
        return ids


def get_persona(persona_id: int) -> dict | None:
    """Get persona information from Spinitron API.

    Args:
        persona_id: The ID of the persona to fetch.

    Returns:
        The persona information, or None if an error occurred.
    """
    logger.debug("Fetching persona with ID `%s`", persona_id)
    return api_get(f"personas/{persona_id}")


def send_discord_notification(payload: dict[str, Any]) -> None:
    """Sends a pre-formatted Discord notification using a webhook.

    Args:
        payload: The payload to send to the Discord webhook. Should contain
            embeds with notification details.
    """
    if not app_config.discord_webhook_url:
        logger.warning(
            "DISCORD_WEBHOOK_URL not configured. Cannot send Discord notification."
        )
        return

    # Ensure timestamp is always present and in UTC
    if payload.get("embeds"):
        payload["embeds"][0]["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        logger.info("Sending notification to Discord.")
        response = requests.post(
            app_config.discord_webhook_url, json=payload, timeout=10
        )
        response.raise_for_status()
        logger.debug("Discord notification sent successfully.")
    except requests.exceptions.RequestException:
        logger.exception("Error sending Discord webhook")
    except Exception:
        logger.exception("Unexpected error sending Discord notification")


def send_discord_source_change(
    current_source: str,
    playlist_info: dict[str, Any] | None,
    persona_info: dict[str, Any] | None,
) -> None:
    """Sends Discord notification about source change.

    Parameters:
    - current_source (str): The current source (A or B).
    - playlist_info (dict): Information about the current playlist.
    - persona_info (dict): Information about the DJ.
    """
    payload: dict[str, Any] = copy.deepcopy(DISCORD_EMBED_PAYLOAD_BASE)
    embed = payload["embeds"][0]

    fields = []
    thumb_url = None

    if playlist_info:
        thumb_url = playlist_info.get("image")
        start_time_str = "N/A"
        if playlist_info.get("start"):
            try:
                # Spinitron 'start' and 'end' are typically ISO 8601 UTC strings
                utc_dt = datetime.fromisoformat(playlist_info["start"])

                # If fromisoformat results in a naive datetime (no tzinfo), assume UTC
                if utc_dt.tzinfo is None or utc_dt.tzinfo.utcoffset(utc_dt) is None:
                    utc_dt = utc_dt.replace(tzinfo=timezone.utc)  # Make it UTC aware

                start_time_str = utc_dt.strftime("%Y-%m-%d %H:%M UTC")
            except ValueError as e:
                logger.warning(
                    "Could not parse start time from Spinitron: %s - %s",
                    playlist_info["start"],
                    e,
                )
            except Exception:
                logger.exception(
                    "Error converting start time %s to %s",
                    playlist_info["start"],
                    app_config.timezone,
                )

        end_time_str = "N/A"
        if playlist_info.get("end"):
            try:
                utc_dt = datetime.fromisoformat(playlist_info["end"])

                if utc_dt.tzinfo is None or utc_dt.tzinfo.utcoffset(utc_dt) is None:
                    utc_dt = utc_dt.replace(tzinfo=timezone.utc)

                end_time_str = utc_dt.strftime("%Y-%m-%d %H:%M UTC")
            except ValueError as e:
                logger.warning(
                    "Could not parse end time from Spinitron: %s - %s",
                    playlist_info["end"],
                    e,
                )
            except Exception:
                logger.exception(
                    "Error converting end time %s to %s",
                    playlist_info["end"],
                    app_config.timezone,
                )
        fields.append(
            {
                "name": "Playlist",
                # Value is a link to the playlist on Spinitron
                "value": (
                    f"[{playlist_info.get('title', 'N/A')}]("
                    f"{app_config.spinitron_api_base_url}/playlists/{playlist_info['id']})"
                    if app_config.spinitron_api_base_url and playlist_info.get("id")
                    else playlist_info.get("title", "N/A")
                ),
            }
        )
        if persona_info and persona_info.get("name"):
            dj_name = persona_info["name"]
            dj_id = persona_info.get("id")
            dj_value = (
                f"[{dj_name}]({app_config.spinitron_api_base_url}/personas/{dj_id})"
                if app_config.spinitron_api_base_url and dj_id
                else dj_name
            )
            fields.append({"name": "DJ", "value": dj_value})
        fields.append(
            {"name": "Playlist Start", "value": start_time_str, "inline": True}
        )
        fields.append({"name": "Playlist End", "value": end_time_str, "inline": True})

    if current_source == BACKUP_SOURCE:
        payload["content"] = "@everyone - Stream May Be Down!"  # Ping everyone
        embed["color"] = DISCORD_EMBED_ERROR_COLOR
        embed["title"] = "FAILSAFE ACTIVATED (Backup Source)"
        embed["description"] = (
            f"Switched to backup source **`{current_source}`**. "
            "Primary source may have failed. Investigate this!"
        )
    else:
        # Switched back to primary
        embed["color"] = DISCORD_EMBED_SUCCESS_COLOR
        embed["title"] = "Failsafe Resolved (Primary Source)"
        embed["description"] = (
            f"Switched back to primary source **`{current_source}`**. System normal."
        )

    if fields:
        embed["fields"] = fields
    if (
        thumb_url and isinstance(thumb_url, str) and embed.get("thumbnail") is None
    ):  # Check if thumbnail is not already set by base
        embed["thumbnail"] = {"url": thumb_url}

    embed["footer"] = {"text": "Powered by wbor-fm/wbor-failsafe-notifier"}

    send_discord_notification(payload)


def send_discord_email_alert(
    dj_name: str,
    dj_email: str,
    playlist_title: str | None,
    playlist_url: str | None,
) -> None:
    """Sends a Discord notification about the email sent to the DJ.

    Parameters:
    - dj_name (str): The name of the DJ.
    - dj_email (str): The email address of the DJ.
    - playlist_title (str): The title of the playlist.
    """
    payload: dict[str, Any] = copy.deepcopy(DISCORD_EMBED_PAYLOAD_BASE)
    embed: dict[str, Any] = payload["embeds"][0]
    embed["title"] = "Failsafe Gadget - DJ Email Notification Sent"
    embed["color"] = DISCORD_EMBED_WARNING_COLOR
    embed["description"] = (
        f"An automated email was sent to **{dj_name}** (`{dj_email}`) "
        "regarding the failsafe activation for their show. Check if the DJ is "
        "aware and needs assistance."
    )

    playlist_str = (
        f"[{playlist_title}]({playlist_url})" if playlist_url else playlist_title
    )

    if playlist_title:
        embed["fields"] = [{"name": "Playlist Currently On-Air", "value": playlist_str}]
    send_discord_notification(payload)


def resolve_and_notify_dj(
    playlist: dict[str, Any], current_source: str
) -> dict[str, Any] | None:
    """Resolves the DJ's email and sends notifications if necessary.

    Parameters:
    - playlist (dict): The current playlist data (unmodified from
        Spinitron).
    - current_source (str): The current source (A or B).

    Returns:
    - dict: Information about the DJ, including email and name.
    """
    if not playlist or playlist.get("automation") is True:  # Explicitly check for True
        logger.info(
            "Current playlist is automation or not found. No DJ specific notifications."
        )
        return None

    persona_id = playlist.get("persona_id")
    primary_persona_data: dict[str, Any] | None = None
    dj_email_to_notify: str | None = None
    dj_name_to_notify: str = "Unknown DJ"

    if persona_id:
        primary_persona_data = get_persona(persona_id)
        if primary_persona_data:
            dj_name_to_notify = primary_persona_data.get("name", "Unknown DJ")
            dj_email_to_notify = primary_persona_data.get("email")

    # Try to find email from other show personas if primary doesn't have one
    if not dj_email_to_notify and playlist.get("show_id"):
        show = get_show(playlist["show_id"])
        if show:
            alt_persona_ids = get_show_persona_ids(show)
            for alt_id in alt_persona_ids:
                if alt_id == persona_id:
                    continue  # Skip primary already checked
                alt_persona = get_persona(alt_id)
                if alt_persona and alt_persona.get("email"):
                    dj_email_to_notify = alt_persona["email"]
                    # Prefer name of persona with email if primary name was "Unknown"
                    # or primary had no email
                    if dj_name_to_notify == "Unknown DJ" or not (
                        primary_persona_data and primary_persona_data.get("email")
                    ):
                        dj_name_to_notify = alt_persona.get("name", "Unknown DJ")
                    logger.info(
                        "Found email via alternative persona: %s", dj_name_to_notify
                    )
                    break

    # Package persona info for Discord and RabbitMQ
    # Ensure persona_info is created even if some details are missing
    persona_info_for_event = {
        "id": persona_id,
        "name": dj_name_to_notify,
        "email_notified": None,  # Will be set if email is sent
    }
    if primary_persona_data:  # Add more details if available
        persona_info_for_event.update(
            {
                k: primary_persona_data.get(k)
                for k in ["website", "bio", "image"]
                if primary_persona_data.get(k)
            }
        )

    # Only notify DJ actively if switched to backup
    if current_source == BACKUP_SOURCE:
        if dj_email_to_notify:
            logger.info(
                "Attempting to email DJ `%s` at `%s`",
                dj_name_to_notify,
                dj_email_to_notify,
            )
            email_body = (
                f"Hey {dj_name_to_notify}!\n\nThis is an automated message from WBOR's "
                "Failsafe Gadget.\n\nIt appears the station has switched to the backup "
                f"audio source during your show '{playlist.get('title', 'N/A')}'. This "
                "usually means there's an issue with the audio from the audio console "
                "(e.g., dead air, incorrect input selected, or equipment malfunction). "
                "\n\n Please check the following:\n"
                "1. Is your microphone on and audible?\n"
                "2. Is your music/audio source playing correctly through the board?\n"
                "3. Are the correct channels selected and faders up on the console?\n\n"
                "If you cannot resolve the issue, please ensure the station is "
                "broadcasting something (e.g., put automation on if available and "
                "working, or a long, clean music track) and contact station management "
                "immediately for assistance.\n\n"
                "Do not reply to this email as it is unattended. Contact management at "
                "wbor@bowdoin.edu or other channels.\n\nThanks for your help!"
                "\n-mgmt\n\n"
                "Automated message sent by wbor-fm/wbor-failsafe-notifier"
            )
            send_email(
                subject="ATTN: WBOR Failsafe Activated - Action Required During "
                "Your Show",
                body=email_body,
                to_email=dj_email_to_notify,
            )

            playlist_url = (
                f"{app_config.spinitron_api_base_url}/playlists/{playlist.get('id')}"
                if app_config.spinitron_api_base_url and playlist.get("id")
                else None
            )
            send_discord_email_alert(
                dj_name_to_notify,
                dj_email_to_notify,
                playlist.get("title"),
                playlist_url,
            )
            persona_info_for_event["email_notified"] = dj_email_to_notify
        else:
            logger.warning(
                "No email address found for DJ(s) of show `%s`. Sending alert to "
                "public GroupMe.",
                playlist.get("title", "N/A"),
            )
            if app_config.groupme_bot_id_djs:
                send_groupme_notification(
                    current_source,
                    app_config.groupme_bot_id_djs,
                    is_public_dj_alert=True,
                )
            else:
                logger.warning(
                    "GROUPME_BOT_ID_DJS not configured, cannot send public DJ alert."
                )
    return persona_info_for_event


def send_groupme_notification(
    current_source: str, bot_id: str | None, *, is_public_dj_alert: bool = False
) -> None:
    """Sends a message to a GroupMe group.

    Parameters:
    - current_source (str): The current source (A or B).
    - bot_id (str): The GroupMe bot ID to send the message to.
    - is_public_dj_alert (bool): Whether the alert is for public DJs or management.
    """
    if not bot_id:
        logger.debug(
            "GroupMe Bot ID not configured for this alert type. Skipping notification."
        )
        return
    if not app_config.groupme_api_base_url:
        logger.warning(
            "`GROUPME_API_BASE_URL` not configured. Cannot send GroupMe message."
        )
        return

    text_message = ""
    if current_source == BACKUP_SOURCE:
        if is_public_dj_alert:
            text_message = (
                "⚠️ WARNING ⚠️\n\nWBOR may be experiencing dead air (more than 60 "
                "seconds of silence). The studio audio console has automatically "
                "switched to the backup audio source. If you are the current DJ, "
                "please check your broadcast. Ensure your microphone is on, music is "
                "playing, and levels are appropriate. If issues persist, contact "
                "station management. Please do not leave the station until management "
                "is contacted."
            )
        else:  # Management alert
            text_message = (
                f"⚠️ FAILSAFE ACTIVATED ⚠️\nWBOR has switched to backup source "
                f"`{current_source}`. "
                "Primary source may have failed. Investigate this!"
            )
    else:  # Switched back to primary
        text_message = (
            f"✅ FAILSAFE RESOLVED ✅\nWBOR has switched back to primary source "
            f"`{current_source}`. "
            "System normal."
        )
    payload = {"bot_id": bot_id, "text": text_message}
    try:
        logger.info(
            "Sending GroupMe notification to bot ID `%s`...",
            bot_id[:5] if bot_id else "N/A",
        )
        response = requests.post(
            f"{app_config.groupme_api_base_url.rstrip('/')}/bots/post",
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.debug("GroupMe message sent successfully.")
    except requests.exceptions.RequestException:
        logger.exception(
            "Error sending GroupMe message to bot ID `%s`...",
            bot_id[:5] if bot_id else "N/A",
        )


def send_all_source_change_notifications(
    current_source: str,
    prev_source: str,
    *,
    current_pin_state: bool,
    local_rabbitmq_publishers: dict[str, RabbitMQPublisher | None],
) -> None:
    """Send all configured notifications for a source change."""
    playlist_data: dict[str, Any] | None = None
    persona_data: dict[str, Any] | None = None

    # Only fetch Spinitron if configured
    if app_config.spinitron_api_base_url:
        playlist_data = get_current_playlist()
        if playlist_data:
            persona_data = resolve_and_notify_dj(playlist_data, current_source)

    # Send Discord Source Change Notification
    if app_config.discord_webhook_url:
        send_discord_source_change(current_source, playlist_data, persona_data)

    # Send GroupMe Management Notification
    if app_config.groupme_bot_id_mgmt:
        send_groupme_notification(
            current_source,
            app_config.groupme_bot_id_mgmt,
            is_public_dj_alert=False,
        )

    # Send RabbitMQ notification
    notifications_publisher = local_rabbitmq_publishers.get("notifications")
    if notifications_publisher:
        rabbitmq_payload = {
            "source_application": "wbor-failsafe-notifier",
            "event_type": "source_change",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "pin_name": PIN_NAME,
            "current_pin_state": current_pin_state,
            "active_source": current_source,
            "previous_active_source": prev_source,
            "details": {
                "playlist": playlist_data if playlist_data else {},
                "persona": persona_data if persona_data else {},
            },
        }
        routing_key = app_config.notifications_exchange.routing_keys["source_change"]
        logger.info(
            "Publishing source change event to RabbitMQ with routing key `%s`.",
            routing_key,
        )
        if not notifications_publisher.publish(routing_key, rabbitmq_payload):
            logger.error("Failed to publish source change event to RabbitMQ.")


def main_loop(
    local_rabbitmq_publishers: dict[str, RabbitMQPublisher | None],
    local_rabbitmq_consumer: RabbitMQConsumer | None,
) -> None:
    """Monitor digital pin and send webhook on state change.

    Main monitoring loop that continuously checks the digital pin state and triggers
    notifications when failsafe source switching occurs.

    Args:
        local_rabbitmq_publishers: Dictionary of RabbitMQ publishers for
            different exchanges.
        local_rabbitmq_consumer: RabbitMQ consumer for receiving override commands.
    """
    prev_pin_state = DIGITAL_PIN.value
    prev_source = PRIMARY_SOURCE if prev_pin_state else BACKUP_SOURCE
    logger.info(
        "%s initial state is %s (input source `%s`)",
        PIN_NAME,
        prev_pin_state,
        prev_source,
    )

    # Start the RabbitMQ consumer if available
    # (Used for override commands)
    if local_rabbitmq_consumer:
        if local_rabbitmq_consumer.start_consuming():
            logger.info("RabbitMQ consumer started successfully")
        else:
            logger.error("Failed to start RabbitMQ consumer")

    # The core of the app: wait for the pin to change state
    while True:
        try:
            # Check for override expiry and handle pending notifications
            override_just_expired = check_override_expiry()

            send_health_check(local_rabbitmq_publishers)

            current_pin_state = DIGITAL_PIN.value
            current_source = PRIMARY_SOURCE if current_pin_state else BACKUP_SOURCE

            # Always detect and log state changes
            if current_pin_state != prev_pin_state:
                logger.info(
                    "Source changed from `%s` (pin: %s) to `%s` (pin: %s)",
                    prev_source,
                    prev_pin_state,
                    current_source,
                    current_pin_state,
                )

                if override_manager.active:
                    # Capture original source when override first becomes active
                    if override_manager.original_source_before_override is None:
                        override_manager.original_source_before_override = prev_source

                    # Log once that override is active during state change
                    if not override_manager.override_logged:
                        logger.info(
                            "Override is active, notifications suppressed for this "
                            "state change"
                        )
                        override_manager.override_logged = True
                    # Track that state changed during override
                    override_manager.state_changed_during_override = True
                    override_manager.pending_source_change = (
                        prev_source,
                        current_source,
                    )
                else:
                    # Send notifications normally when override is not active
                    send_all_source_change_notifications(
                        current_source,
                        prev_source,
                        current_pin_state=current_pin_state,
                        local_rabbitmq_publishers=local_rabbitmq_publishers,
                    )

            # Handle notifications after override expires
            elif (
                override_just_expired and override_manager.state_changed_during_override
            ):
                # Only send notification if current source differs from original source
                if (
                    override_manager.original_source_before_override is not None
                    and current_source
                    != override_manager.original_source_before_override
                ):
                    logger.info(
                        "Override expired, sending delayed notification for source "
                        "change from `%s` to `%s`",
                        override_manager.original_source_before_override,
                        current_source,
                    )
                    send_all_source_change_notifications(
                        current_source,
                        override_manager.original_source_before_override,
                        current_pin_state=current_pin_state,
                        local_rabbitmq_publishers=local_rabbitmq_publishers,
                    )
                else:
                    logger.info(
                        "Override expired, but source returned to original state (%s), "
                        "no notification needed",
                        override_manager.original_source_before_override or "unknown",
                    )
                override_manager.state_changed_during_override = False
                override_manager.pending_source_change = None
                override_manager.original_source_before_override = None

            # Update state tracking
            prev_pin_state = current_pin_state
            prev_source = current_source

            time.sleep(0.5)  # Check interval for pin state

        except Exception:
            logger.exception("Error in main monitoring loop")
            time.sleep(10)


def main() -> None:
    """Primary entry point for the WBOR Failsafe Notifier.

    Initializes RabbitMQ connections, sets up signal handlers for graceful shutdown, and
    starts the main monitoring loop. Handles cleanup on exit.
    """
    local_rabbitmq_publishers: dict[str, RabbitMQPublisher | None] = {}
    local_rabbitmq_consumer: RabbitMQConsumer | None = None
    try:
        if DRY_RUN:
            logger.info("Starting WBOR Failsafe Notifier in DRY_RUN mode (CI)...")
            logger.info(
                "Primary Source: `%s`, Backup Source: `%s`, Monitored Pin: `%s`",
                PRIMARY_SOURCE,
                BACKUP_SOURCE,
                PIN_NAME,
            )

            # Don't initialize RabbitMQ or GPIO in DRY_RUN mode
            logger.info("Skipping RabbitMQ and GPIO initialization in DRY_RUN mode.")
            logger.info("Skipping main loop in DRY_RUN mode.")
            logger.info("WBOR Failsafe Notifier DRY_RUN mode complete.")
            return

        logger.info("Starting WBOR Failsafe Notifier...")
        logger.info(
            "Primary Source: `%s`, Backup Source: `%s`, Monitored Pin: `%s`",
            PRIMARY_SOURCE,
            BACKUP_SOURCE,
            PIN_NAME,
        )

        local_rabbitmq_publishers = initialize_rabbitmq_publishers()
        local_rabbitmq_consumer = initialize_rabbitmq_consumer()
        main_loop(local_rabbitmq_publishers, local_rabbitmq_consumer)

    except ValueError as e:
        logger.critical("Configuration error: %s. Exiting.", e)
    except RuntimeError as e:
        logger.critical("Runtime initialization error: %s. Exiting.", e)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    except Exception as e:
        logger.critical("An unexpected critical error occurred: %s", e, exc_info=True)
    finally:
        logger.info("WBOR Failsafe Notifier shutting down.")
        if local_rabbitmq_consumer:
            local_rabbitmq_consumer.stop_consuming()
        for publisher in local_rabbitmq_publishers.values():
            if publisher:
                publisher.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
