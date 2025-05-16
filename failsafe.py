"""
Monitors a digital input on a microcontroller board and sends a Discord
webhook notification when the input state changes. It distinguishes
between primary and backup sources based on the configured pin state.

Author: Mason Daugherty <@mdrxy>
Version: 1.3.3
Last Modified: 2025-05-10

Changelog:
    - 1.0.0 (2025-03-22): Initial release.
    - 1.2.0 (2025-05-06): Many fixes and small enhancements
    - 1.3.0 (2025-05-10): Added RabbitMQ publishing for notifications
        and refactored code for better readability and maintainability.
    - 1.3.1 (2025-05-10): Refactored to remove global statement for
        RabbitMQ publisher.
    - 1.3.2 (2025-05-10): Timezone and wording fixes
    - 1.3.3 (2025-05-16): Link the playlist in the email notification
        embed
"""

import copy
import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

import board
import digitalio
import pytz
import requests
from dotenv import dotenv_values

from utils.logging import configure_logging
from utils.rabbitmq_publisher import RabbitMQPublisher

logging.root.handlers = []
logger = configure_logging()

# Load and validate required configurations
config = dotenv_values(".env")
if not config:
    logger.warning(
        ".env file is empty or not found. Attempting to use system environment variables."
    )
    import os as _os

    config = _os.environ


required_configs = [
    "PIN_ASSIGNMENT",
    "BACKUP_INPUT",
]
missing_configs = [key for key in required_configs if not config.get(key)]
if missing_configs:
    CFG_ERR_MSG = (
        f"Required configuration(s) `{'`, `'.join(missing_configs)}` must be set in .env file or "
        "environment!"
    )
    logger.critical(CFG_ERR_MSG)
    raise ValueError(CFG_ERR_MSG)

PIN_NAME = config.get("PIN_ASSIGNMENT")
if PIN_NAME is None:
    logger.critical("PIN_ASSIGNMENT is not set in the configuration.")
    raise ValueError("PIN_ASSIGNMENT must be set in the configuration.")
try:
    pin = getattr(board, PIN_NAME)
except AttributeError as exc:
    logger.critical("%s is not a valid pin name for this board.", PIN_NAME)
    raise ValueError(f"{PIN_NAME} is not a valid pin name for this board.") from exc
except Exception as e:  # Catch other board related errors, e.g. if Blinka is not setup
    logger.critical(
        "Failed to access board attribute for pin %s: %s", PIN_NAME, e, exc_info=True
    )
    raise RuntimeError(f"Board or pin initialization error for {PIN_NAME}") from e


try:
    DIGITAL_PIN = digitalio.DigitalInOut(pin)
    DIGITAL_PIN.switch_to_input()
except Exception as e:
    logger.critical("Failed to initialize digital pin %s: %s", PIN_NAME, e)
    raise RuntimeError(f"Failed to initialize digital pin {PIN_NAME}") from e

# Determine primary and backup sources.
BACKUP_SOURCE = str(config.get("BACKUP_INPUT", "B")).upper()  # Default to B if not set
PRIMARY_SOURCE = "B" if BACKUP_SOURCE == "A" else "A"
if BACKUP_SOURCE not in ["A", "B"]:  # Check after assignment
    raise ValueError("`BACKUP_INPUT` must be either 'A' or 'B'.")


# Colors (in decimal)
DISCORD_EMBED_ERROR_COLOR = 16711680  # Red
DISCORD_EMBED_WARNING_COLOR = 16776960  # Yellow
DISCORD_EMBED_SUCCESS_COLOR = 65280  # Green

# Discord base payload
DISCORD_WEBHOOK_URL = config.get("DISCORD_WEBHOOK_URL")
DISCORD_EMBED_PAYLOAD_BASE = {
    "embeds": [
        {
            "title": "Failsafe Gadget - Source Switched",
            "author": {
                "name": config.get("AUTHOR_NAME") or "wbor-failsafe-notifier",
                "url": config.get("AUTHOR_URL")
                or "https://github.com/WBOR-91-1-FM/wbor-failsafe-notifier",
                "icon_url": config.get("AUTHOR_ICON_URL") or None,
            },
        }
    ],
}

# Spinitron config
SPINITRON_API_BASE_URL = config.get("SPINITRON_API_BASE_URL")

# GroupMe config
GROUPME_API_BASE_URL = config.get("GROUPME_API_BASE_URL", "https://api.groupme.com/v3")
GROUPME_BOT_ID_MGMT = config.get("GROUPME_BOT_ID_MGMT")
GROUPME_BOT_ID_DJS = config.get("GROUPME_BOT_ID_DJS")

# Email config
SMTP_SERVER = config.get("SMTP_SERVER")
SMTP_PORT = config.get("SMTP_PORT")
SMTP_USERNAME = config.get("SMTP_USERNAME")
SMTP_PASSWORD = config.get("SMTP_PASSWORD")
FROM_EMAIL = config.get("FROM_EMAIL")
ERROR_EMAIL = config.get("ERROR_EMAIL")

# RabbitMQ config
RABBITMQ_AMQP_URL = config.get("RABBITMQ_AMQP_URL")
RABBITMQ_EXCHANGE_NAME = config.get("RABBITMQ_EXCHANGE_NAME") or "wbor_failsafe_events"
RABBITMQ_ROUTING_KEY = config.get(
    "RABBITMQ_ROUTING_KEY",
    "notification.failsafe-status",
)


def initialize_rabbitmq() -> Optional[RabbitMQPublisher]:
    """
    Initializes and returns RabbitMQPublisher if configured.
    """
    if RABBITMQ_AMQP_URL:
        try:
            publisher = RabbitMQPublisher(
                amqp_url=RABBITMQ_AMQP_URL, exchange_name=RABBITMQ_EXCHANGE_NAME
            )
            logger.info(
                "RabbitMQ publisher initialized for exchange `%s`.",
                RABBITMQ_EXCHANGE_NAME,
            )
            return publisher
        except Exception as e:  # pylint: disable=broad-except
            logger.error(
                "Failed to initialize RabbitMQ publisher: `%s`. Proceeding without RabbitMQ.",
                e,
                exc_info=True,
            )
    else:
        logger.info(
            "RabbitMQ AMQP URL not configured. RabbitMQ publishing will be disabled."
        )
    return None


def api_get(endpoint: str) -> Optional[dict]:
    """
    Make a GET request to the Spinitron API and return the JSON
    response.

    Parameters:
    - endpoint (str): The API endpoint to fetch.

    Returns:
    - dict: The JSON response from the API, or None if an error
        occurred.
    """
    if not SPINITRON_API_BASE_URL:
        logger.warning(
            "SPINITRON_API_BASE_URL not configured. Cannot make API GET request."
        )
        return None
    url = f"{SPINITRON_API_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        logger.error(
            "HTTP error fetching `%s`: Status %s, Response: %s",
            url,
            e.response.status_code,
            e.response.text,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Request error fetching `%s`: `%s`", url, e)
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Unexpected error fetching `%s`: `%s`", url, e, exc_info=True)
    return None


def send_email(subject: str, body: str, to_email: str) -> None:
    """
    Send an email using the configured SMTP server.

    Parameters:
    - subject (str): The subject of the email.
    - body (str): The body of the email.
    - to_email (str): The recipient's email address.
    """
    logger.info("Attempting to send email to `%s` with subject: %s", to_email, subject)

    if not SMTP_SERVER:
        logger.error("SMTP_SERVER is not set, cannot send email.")
        return
    if not SMTP_USERNAME:
        logger.error("SMTP_USERNAME is not set, cannot send email.")
        return
    if not SMTP_PASSWORD:
        logger.error("SMTP_PASSWORD is not set, cannot send email.")
        return
    if not FROM_EMAIL:
        logger.error("FROM_EMAIL is not set, cannot send email.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["To"] = to_email
        msg["From"] = FROM_EMAIL

        smtp_port_int = (
            int(SMTP_PORT) if SMTP_PORT else 587
        )  # Default to 587 if not set (common for TLS)

        with smtplib.SMTP(SMTP_SERVER, smtp_port_int, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        logger.info("Email successfully sent to `%s`", to_email)
    except smtplib.SMTPRecipientsRefused as e:
        logger.error("SMTP recipients refused for `%s`: %s", to_email, e.recipients)
        if ERROR_EMAIL and to_email != ERROR_EMAIL:  # Avoid self-notification loop
            send_email(
                subject="Failsafe Gadget - SMTP Recipients Refused",
                body=f"SMTP recipients refused for {to_email}: {e.recipients}",
                to_email=ERROR_EMAIL,
            )
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Failed to send email to %s: %s", to_email, e, exc_info=True)
        if ERROR_EMAIL and to_email != ERROR_EMAIL:
            send_email(
                subject="Failsafe Gadget - Email Sending Failure",
                body=f"General failure sending email to {to_email}: {e}",
                to_email=ERROR_EMAIL,
            )


def get_current_playlist() -> Optional[dict]:
    """
    Get the current playlist from Spinitron API.
    """
    logger.debug("Fetching current playlist from Spinitron API")
    data = api_get("playlists?count=1")  # Fetch only the latest one
    if data:
        items = data.get("items", [])
        if items:
            playlist = items[0]
            logger.debug("Current playlist: `%s`", playlist.get("title", "N/A"))
            return playlist
        logger.warning("No playlist items found in the response: %s", data)
    return None


def get_show(show_id: int) -> Optional[dict]:
    """
    Get show information from Spinitron API.

    Parameters:
    - show_id (int): The ID of the show to fetch.

    Returns:
    - dict: The show information, or None if an error occurred.
    """
    logger.debug("Fetching show with ID `%s`", show_id)
    return api_get(f"shows/{show_id}")


def get_show_persona_ids(show: dict) -> list[int]:
    """
    Get persona IDs from a show object.
    Extracts persona IDs from the `_links` field of the show object.

    Parameters:
    - show (dict): The show object containing persona links.

    Returns:
    - list[int]: A list of persona IDs extracted from the show object.
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
        return ids
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Error parsing persona IDs from show: `%s`", e, exc_info=True)
    return []


def get_persona(persona_id: int) -> Optional[dict]:
    """
    Get persona information from Spinitron API.

    Parameters:
    - persona_id (int): The ID of the persona to fetch.

    Returns:
    - dict: The persona information, or None if an error occurred.
    """
    logger.debug("Fetching persona with ID `%s`", persona_id)
    return api_get(f"personas/{persona_id}")


def send_discord_notification(payload: Dict[str, Any]) -> None:
    """
    Sends a pre-formatted Discord notification using a webhook.

    Parameters:
    - payload (dict): The payload to send to the Discord webhook.
    """
    if not DISCORD_WEBHOOK_URL:
        logger.warning(
            "DISCORD_WEBHOOK_URL not configured. Cannot send Discord notification."
        )
        return

    # Ensure timestamp is always present and in UTC
    if "embeds" in payload and payload["embeds"]:
        payload["embeds"][0]["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        logger.info("Sending notification to Discord.")
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        logger.debug("Discord notification sent successfully.")
    except requests.exceptions.RequestException as e:
        logger.error("Error sending Discord webhook: %s", e)
    except Exception as e:  # pylint: disable=broad-except
        logger.error(
            "Unexpected error sending Discord notification: %s", e, exc_info=True
        )


def send_discord_source_change(
    current_source: str,
    playlist_info: Optional[Dict[str, Any]],
    persona_info: Optional[Dict[str, Any]],
) -> None:
    """
    Sends Discord notification about source change.

    Parameters:
    - current_source (str): The current source (A or B).
    - playlist_info (dict): Information about the current playlist.
    - persona_info (dict): Information about the DJ.
    """
    payload: Dict[str, Any] = copy.deepcopy(DISCORD_EMBED_PAYLOAD_BASE)
    embed = payload["embeds"][0]

    fields = []
    thumb_url = None
    eastern_tz = pytz.timezone("America/New_York")

    if playlist_info:
        thumb_url = playlist_info.get("image")
        start_time_str = "N/A"
        if playlist_info.get("start"):
            try:
                # Spinitron 'start' and 'end' are typically ISO 8601 UTC strings
                utc_dt = datetime.fromisoformat(playlist_info["start"])

                # If fromisoformat results in a naive datetime (no tzinfo), assume it's UTC
                if utc_dt.tzinfo is None or utc_dt.tzinfo.utcoffset(utc_dt) is None:
                    utc_dt = utc_dt.replace(tzinfo=timezone.utc)  # Make it UTC aware

                eastern_dt = utc_dt.astimezone(eastern_tz)  # Convert to Eastern Time
                start_time_str = eastern_dt.strftime(
                    "%Y-%m-%d %I:%M %p %Z"
                )  # %Z will add EST/EDT
            except ValueError as e:
                logger.warning(
                    "Could not parse start time from Spinitron: %s - %s",
                    playlist_info["start"],
                    e,
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.error(
                    "Error converting start time %s to Eastern: %s",
                    playlist_info["start"],
                    e,
                    exc_info=True,
                )

        end_time_str = "N/A"
        if playlist_info.get("end"):
            try:
                utc_dt = datetime.fromisoformat(playlist_info["end"])

                if utc_dt.tzinfo is None or utc_dt.tzinfo.utcoffset(utc_dt) is None:
                    utc_dt = utc_dt.replace(tzinfo=timezone.utc)

                eastern_dt = utc_dt.astimezone(eastern_tz)
                end_time_str = eastern_dt.strftime(
                    "%Y-%m-%d %I:%M %p %Z"
                )  # %Z will add EST/EDT
            except ValueError as e:
                logger.warning(
                    "Could not parse end time from Spinitron: %s - %s",
                    playlist_info["end"],
                    e,
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.error(
                    "Error converting end time %s to Eastern: %s",
                    playlist_info["end"],
                    e,
                    exc_info=True,
                )
        fields.append(
            {
                "name": "Playlist",
                # Value is a link to the playlist on Spinitron
                "value": (
                    f"[{playlist_info.get('title', 'N/A')}]({SPINITRON_API_BASE_URL}/playlists/"
                    f"{playlist_info['id']})"
                    if SPINITRON_API_BASE_URL and playlist_info.get("id")
                    else playlist_info.get("title", "N/A")
                ),
            }
        )
        if persona_info and persona_info.get("name"):
            dj_name = persona_info["name"]
            dj_id = persona_info.get("id")
            dj_value = (
                f"[{dj_name}]({SPINITRON_API_BASE_URL}/personas/{dj_id})"
                if SPINITRON_API_BASE_URL and dj_id
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

    embed["footer"] = {"text": "Powered by WBOR-91-1-FM/wbor-failsafe-notifier"}

    send_discord_notification(payload)


def send_discord_email_alert(
    dj_name: str,
    dj_email: str,
    playlist_title: Optional[str],
    playlist_url: Optional[str],
) -> None:
    """
    Sends a Discord notification about the email sent to the DJ.

    Parameters:
    - dj_name (str): The name of the DJ.
    - dj_email (str): The email address of the DJ.
    - playlist_title (str): The title of the playlist.
    """
    payload = copy.deepcopy(DISCORD_EMBED_PAYLOAD_BASE)
    embed = payload["embeds"][0]
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
    playlist: Dict[str, Any], current_source: str
) -> Optional[Dict[str, Any]]:
    """
    Resolves the DJ's email and sends notifications if necessary.

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
    primary_persona_data: Optional[Dict[str, Any]] = None
    dj_email_to_notify: Optional[str] = None
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
                    # Prefer name of persona with email if primary name was "Unknown" or primary had
                    # no email
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
                f"Hey {dj_name_to_notify}!\n\nThis is an automated message from WBOR's Failsafe "
                "Gadget.\n\nIt appears the station has switched to the backup audio source during "
                f"your show '{playlist.get('title', 'N/A')}'. This usually means there's an issue "
                "with the audio from the audio console (e.g., dead air, incorrect input selected, "
                "or equipment malfunction).\n\n Please check the following:\n"
                "1. Is your microphone on and audible?\n"
                "2. Is your music/audio source playing correctly through the board?\n"
                "3. Are the correct channels selected and faders up on the mixing console?\n\n"
                "If you cannot resolve the issue, please ensure the station is broadcasting "
                "something (e.g., put automation on if available and working, or a long, clean "
                "music track) and contact station management immediately for assistance.\n\n"
                "Do not reply to this email as it is unattended. Contact management via "
                "wbor@bowdoin.edu or other channels.\n\nThanks for your help!\n-mgmt\n\n"
                "Automated message sent by WBOR-91-1-FM/wbor-failsafe-notifier"
            )
            send_email(
                subject="ATTN: WBOR Failsafe Activated - Action Required During Your Show",
                body=email_body,
                to_email=dj_email_to_notify,
            )

            playlist_url = (
                f"{SPINITRON_API_BASE_URL}/playlists/{playlist.get('id')}"
                if SPINITRON_API_BASE_URL and playlist.get("id")
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
                "No email address found for DJ(s) of show `%s`. Sending alert to public GroupMe.",
                playlist.get("title", "N/A"),
            )
            if GROUPME_BOT_ID_DJS:
                send_groupme_notification(
                    current_source, GROUPME_BOT_ID_DJS, is_public_dj_alert=True
                )
            else:
                logger.warning(
                    "GROUPME_BOT_ID_DJS not configured, cannot send public DJ alert."
                )
    return persona_info_for_event


def send_groupme_notification(
    current_source: str, bot_id: Optional[str], is_public_dj_alert: bool = False
) -> None:
    """
    Sends a message to a GroupMe group.

    Parameters:
    - current_source (str): The current source (A or B).
    - bot_id (str): The GroupMe bot ID to send the message to.
    - is_public_dj_alert (bool): Whether the alert is for public DJs or management.
    """
    if not bot_id:
        logger.debug(
            "GroupMe Bot ID not configured for this alert type. Skipping GroupMe notification."
        )
        return
    if not GROUPME_API_BASE_URL:
        logger.warning(
            "`GROUPME_API_BASE_URL` not configured. Cannot send GroupMe message."
        )
        return

    text_message = ""
    if current_source == BACKUP_SOURCE:
        if is_public_dj_alert:
            text_message = (
                "⚠️ WARNING ⚠️\n\nWBOR may be experiencing dead air (more than 60 seconds of "
                "silence). The studio audio console has automatically switched to the backup audio "
                "source. If you are the current DJ, please check your broadcast. Ensure your "
                "microphone is on, music is playing, and levels are appropriate. If issues "
                "persist, contact station management. Please do not leave the station "
                "until management is contacted."
            )
        else:  # Management alert
            text_message = (
                f"⚠️ FAILSAFE ACTIVATED ⚠️\nWBOR has switched to backup source `{current_source}`. "
                "Primary source may have failed. Investigate this!"
            )
    else:  # Switched back to primary
        text_message = (
            f"✅ FAILSAFE RESOLVED ✅\nWBOR has switched back to primary source `{current_source}`. "
            "System normal."
        )
    payload = {"bot_id": bot_id, "text": text_message}
    try:
        logger.info(
            "Sending GroupMe notification to bot ID `%s`...",
            bot_id[:5] if bot_id else "N/A",
        )
        response = requests.post(
            f"{GROUPME_API_BASE_URL.rstrip('/')}/bots/post", json=payload, timeout=10
        )
        response.raise_for_status()
        logger.debug("GroupMe message sent successfully.")
    except requests.exceptions.RequestException as e:
        logger.error(
            "Error sending GroupMe message to bot ID `%s`...: %s",
            bot_id[:5] if bot_id else "N/A",
            e,
        )
        logger.error("Unexpected error sending GroupMe message: %s", e, exc_info=True)


def main_loop(
    local_rabbitmq_publisher: Optional[RabbitMQPublisher],
):
    """
    Monitor digital pin and send webhook on state change.
    """
    prev_pin_state = DIGITAL_PIN.value
    prev_source = PRIMARY_SOURCE if prev_pin_state else BACKUP_SOURCE
    logger.info(
        "%s initial state is %s (input source `%s`)",
        PIN_NAME,
        prev_pin_state,
        prev_source,
    )

    # Wait for the pin to change state
    while True:
        try:
            current_pin_state = DIGITAL_PIN.value
            current_source = PRIMARY_SOURCE if current_pin_state else BACKUP_SOURCE

            if current_pin_state != prev_pin_state:
                logger.info(
                    "Source changed from `%s` (pin: %s) to `%s` (pin: %s)",
                    prev_source,
                    prev_pin_state,
                    current_source,
                    current_pin_state,
                )

                playlist_data: Optional[Dict[str, Any]] = None
                persona_data: Optional[Dict[str, Any]] = None

                # Only fetch Spinitron if configured
                if SPINITRON_API_BASE_URL:
                    playlist_data = get_current_playlist()
                    if playlist_data:
                        persona_data = resolve_and_notify_dj(
                            playlist_data, current_source
                        )

                # Send Discord Source Change Notification
                if DISCORD_WEBHOOK_URL:
                    send_discord_source_change(
                        current_source, playlist_data, persona_data
                    )

                # Send GroupMe Management Notification
                if GROUPME_BOT_ID_MGMT:
                    send_groupme_notification(
                        current_source, GROUPME_BOT_ID_MGMT, is_public_dj_alert=False
                    )

                if local_rabbitmq_publisher:
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
                    logger.info(
                        "Publishing source change event to RabbitMQ with routing key `%s`.",
                        RABBITMQ_ROUTING_KEY,
                    )
                    routing_key = RABBITMQ_ROUTING_KEY or "notification.failsafe-status"
                    if not local_rabbitmq_publisher.publish(
                        routing_key, rabbitmq_payload
                    ):
                        logger.error(
                            "Failed to publish source change event to RabbitMQ."
                        )

                prev_pin_state = current_pin_state
                prev_source = current_source

            time.sleep(0.5)  # Check interval for pin state

        except Exception as e:  # pylint: disable=broad-except
            logger.error("Error in main monitoring loop: %s", e, exc_info=True)
            time.sleep(10)


def main():
    """
    Primary entry point for the WBOR Failsafe Notifier.
    """
    local_rabbitmq_publisher: Optional[RabbitMQPublisher] = None
    try:
        logger.info("Starting WBOR Failsafe Notifier v1.3.3...")
        logger.info(
            "Primary Source: `%s`, Backup Source: `%s`, Monitored Pin: `%s`",
            PRIMARY_SOURCE,
            BACKUP_SOURCE,
            PIN_NAME,
        )

        local_rabbitmq_publisher = initialize_rabbitmq()
        main_loop(local_rabbitmq_publisher)

    except ValueError as e:
        logger.critical("Configuration error: %s. Exiting.", e)
    except RuntimeError as e:
        logger.critical("Runtime initialization error: %s. Exiting.", e)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    except Exception as e:  # pylint: disable=broad-except
        logger.critical("An unexpected critical error occurred: %s", e, exc_info=True)
    finally:
        logger.info("WBOR Failsafe Notifier shutting down.")
        if local_rabbitmq_publisher:
            local_rabbitmq_publisher.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
