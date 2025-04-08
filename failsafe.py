"""
Monitors a digital input on a microcontroller board and sends a Discord
webhook notification when the input state changes. It distinguishes
between primary and backup sources based on the configured pin state.

Author: Mason Daugherty <@mdrxy>
Version: 1.0.0
Last Modified: 2025-03-22

Changelog:
    - 1.0.0 (2025-03-22): Initial release.
"""

import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

import board
import digitalio
import requests
from dotenv import dotenv_values

from utils.logging import configure_logging

logging.root.handlers = []
logger = configure_logging()

config = dotenv_values(".env")
if not config.get("PIN_ASSIGNMENT"):
    raise ValueError("`PIN_ASSIGNMENT` must be set in the .env file!")
if not config.get("DISCORD_WEBHOOK_URL"):
    raise ValueError("`DISCORD_WEBHOOK_URL` must be set in the .env file!")
if not config.get("BACKUP_INPUT"):
    raise ValueError("`BACKUP_INPUT` must be set in the .env file!")

PIN_NAME = config.get("PIN_ASSIGNMENT")
try:
    pin = getattr(board, PIN_NAME)
except AttributeError as exc:
    raise ValueError(f"{PIN_NAME} is not a valid pin name for this board.") from exc

DIGITAL_PIN = digitalio.DigitalInOut(pin)
DIGITAL_PIN.switch_to_input()

# Determine primary and backup sources.
# If BACKUP_INPUT is "A", then primary is "B"; if BACKUP_INPUT is "B",
# then primary is "A".
BACKUP_SOURCE = str(config.get("BACKUP_INPUT")).upper()
PRIMARY_SOURCE = "B" if BACKUP_SOURCE == "A" else "A" if BACKUP_SOURCE == "B" else None
if PRIMARY_SOURCE is None:
    raise ValueError("`BACKUP_INPUT` must be either 'A' or 'B'.")

# Colors (in decimal)
DISCORD_EMBED_ERROR_COLOR = 16711680  # Red
DISCORD_EMBED_WARNING_COLOR = 16776960  # Yellow
DISCORD_EMBED_SUCCESS_COLOR = 65280  # Green

DISCORD_EMBED_PAYLOAD = {
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

SPINITRON_API_BASE_URL = config.get("SPINITRON_API_BASE_URL")

# Helper function for API GET requests


def api_get(endpoint: str) -> dict:
    """
    Make a GET request to the Spinitron API and return the JSON
    response.
    """
    url = f"{SPINITRON_API_BASE_URL}{endpoint}"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error("Error fetching `%s`: `%s`", url, e)
        return None
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Unexpected error fetching `%s`: `%s`", url, e)
        return None


def send_email(
    subject: str, body: str, to: str, from_: str = config.get("FROM_EMAIL")
) -> None:
    """
    Send an email using the configured SMTP server.
    """
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["To"] = to
        msg["From"] = from_

        with smtplib.SMTP(config.get("SMTP_SERVER"), config.get("SMTP_PORT")) as server:
            server.starttls()
            server.login(config.get("SMTP_USERNAME"), config.get("SMTP_PASSWORD"))
            server.sendmail(from_, [to], msg.as_string())
    except smtplib.SMTPRecipientsRefused as e:
        logger.error("SMTP recipients refused: `%s`", e)
        send_email(
            subject="Failsafe Gadget - SMTP Recipients Refused",
            body=f"SMTP recipients refused: {e}",
            to=config.get("ERROR_EMAIL"),
        )
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Failed to send email: %s", e)


def get_current_playlist() -> dict:
    """
    Get the current playlist from Spinitron API.
    """
    logger.debug("Fetching current playlist from Spinitron API")
    data = api_get("/playlists")
    if data:
        items = data.get("items", [])
        if items:
            playlist = items[0]
            logger.debug("Current playlist: `%s`", playlist)
            return playlist
        logger.error("No playlist items found in the response: %s", data)
    return None


def get_show(show_id: int) -> dict:
    """
    Get the show from Spinitron API.
    """
    logger.debug("Fetching show with ID `%s`", show_id)
    return api_get(f"/shows/{show_id}")


def get_show_persona_ids(show: dict) -> list:
    """
    Extract persona IDs from a show response.
    """
    try:
        personas = show.get("_links", {}).get("personas", [])
        return [
            int(p["href"].rstrip("/").split("/")[-1]) for p in personas if p.get("href")
        ]
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Error parsing persona IDs: `%s`", e)
        return []


def get_persona(persona_id: int) -> dict:
    """
    Get the persona from Spinitron API.
    """
    logger.debug("Fetching persona with ID `%s`", persona_id)
    return api_get(f"/personas/{persona_id}")


def send_discord_email_notification(persona: dict) -> None:
    """
    Send a Discord webhook notification that an email was sent to the
    DJ.
    """
    try:
        payload = DISCORD_EMBED_PAYLOAD.copy()
        fields = []
        if persona.get("string"):
            fields.append({"name": "DJ", "value": persona["string"]})
        if persona.get("playlist"):
            playlist = persona["playlist"]
            fields.append(
                {
                    "name": "Playlist",
                    "value": (
                        f"[{playlist['title']}]({SPINITRON_API_BASE_URL}"
                        f"/playlists/{playlist['id']})"
                    ),
                }
            )
        payload["embeds"][0]["title"] = "Failsafe Gadget - Email Sent"
        payload["embeds"][0]["color"] = DISCORD_EMBED_WARNING_COLOR
        payload["embeds"][0]["description"] = (
            f"Email sent to `{persona['email']}` regarding backup source activation. "
            "Check if the DJ is aware and needs assistance."
        )
        if fields:
            payload["embeds"][0]["fields"] = fields
            logger.debug("send_discord_email_notification() Fields: `%s`", fields)
        payload["embeds"][0]["timestamp"] = datetime.now(timezone.utc).isoformat()
        response = requests.post(
            config.get("DISCORD_WEBHOOK_URL"), json=payload, timeout=5
        )
        response.raise_for_status()
        logger.debug("Discord email message sent successfully")
    except requests.exceptions.RequestException as e:
        logger.error("Error sending Discord email webhook: `%s`", e)


def resolve_persona(playlist: dict, current_source: str) -> tuple:
    """
    Resolve DJ persona details from a playlist.
    Returns a tuple (persona_id, persona_name, persona_email,
    persona_str).
    """
    # Get the primary persona details.
    persona = get_persona(playlist["persona_id"])
    if persona:
        pid = persona.get("id")
        name = persona.get("name", "Unknown")
        email = persona.get("email")
        pstr = f"[{name}](mailto:{email})" if email else name
    else:
        pid, name, email, pstr = None, "Unknown", None, "Unknown"

    # If we already have an email, return early.
    if email:
        return pid, name, email, pstr

    # Try to resolve an alternative persona email using the show's
    # personas.
    show_id = playlist.get("show_id")
    if show_id:
        show = get_show(show_id)
        if show:
            alt_ids = [
                alt_pid for alt_pid in get_show_persona_ids(show) if alt_pid != pid
            ]
            for alt_pid in alt_ids:
                alt_persona = get_persona(alt_pid)
                if alt_persona:
                    alt_email = alt_persona.get("email")
                    if alt_email:
                        email = alt_email
                        pstr = f"[{alt_persona.get('name', 'Unknown')}](mailto:{email})"
                        break

    if not email:
        logger.warning(
            "No email address found for DJ %s - sending message to DJ GroupMe group",
            name,
        )

        # Only GroupMe when switching to backup source, not when
        # switching back to primary.
        if current_source == BACKUP_SOURCE:
            send_groupme(
                current_source, public=True, bot_id=config.get("GROUPME_BOT_ID_DJS")
            )

    return pid, name, email, pstr


def send_discord(current_source: str) -> dict:
    """
    Send a Discord webhook with an embed payload based on the source
    state. Returns a dict with playlist and DJ info.
    """
    try:
        payload = DISCORD_EMBED_PAYLOAD.copy()
        fields = []
        playlist = get_current_playlist()

        persona_id = None
        persona_name = None
        persona_email = None
        persona_str = None

        if playlist:
            start_time = datetime.fromisoformat(playlist["start"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            end_time = datetime.fromisoformat(playlist["end"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            is_automation = playlist.get("automation") == "1"
            logger.debug("Playlist: `%s`", playlist)
            if not is_automation:
                persona_id, persona_name, persona_email, persona_str = resolve_persona(
                    playlist, current_source
                )
            fields.extend(
                [
                    {
                        "name": "Playlist",
                        "value": (
                            f"[{playlist['title']}]({SPINITRON_API_BASE_URL}"
                            f"/playlists/{playlist['id']})"
                        ),
                    },
                    (
                        {
                            "name": "DJ",
                            "value": (
                                f"[{persona_str}]({SPINITRON_API_BASE_URL}"
                                f"/personas/{persona_id})"
                            ),
                        }
                        if persona_id
                        else {"name": "DJ", "value": persona_str}
                    ),
                    {"name": "Start", "value": start_time, "inline": True},
                    {"name": "End", "value": end_time, "inline": True},
                ]
            )

        if current_source == BACKUP_SOURCE:
            payload["content"] = "@everyone - stream may be down!"
            payload["embeds"][0]["color"] = DISCORD_EMBED_ERROR_COLOR
            payload["embeds"][0]["description"] = (
                f"⚠️ WARNING ⚠️ switching to backup source `{current_source}`. "
                "This may indicate a failure in the primary source and should be investigated."
            )
            if fields:
                payload["embeds"][0]["fields"] = fields
            else:
                payload["embeds"][0]["description"] += (
                    "\n\nNo playlist information available. "
                    "Please check the Spinitron API for details."
                )
        else:
            payload["embeds"][0]["color"] = DISCORD_EMBED_SUCCESS_COLOR
            payload["embeds"][0][
                "description"
            ] = f"Switched back to primary source `{current_source}`"

        payload["embeds"][0]["timestamp"] = datetime.now(timezone.utc).isoformat()
        response = requests.post(
            config.get("DISCORD_WEBHOOK_URL"), json=payload, timeout=5
        )
        response.raise_for_status()
        logger.debug("Discord message sent successfully")

        return {
            "playlist": playlist,
            "name": persona_name,
            "email": persona_email,
            "string": persona_str,
        }
    except requests.exceptions.RequestException as e:
        logger.error("Error sending Discord webhook: %s", e)
    return None


def send_groupme(
    current_source: str,
    public: bool = False,
    bot_id: str = config.get("GROUPME_BOT_ID_MGMT"),
) -> None:
    """
    Send a message to a GroupMe group.
    """
    try:
        payload = {
            "bot_id": bot_id,
            "text": f"Stream switched back to primary source `{current_source}`",
        }
        if current_source == BACKUP_SOURCE:
            if not public:
                payload["text"] = (
                    f"⚠️ WARNING ⚠️ stream switching to backup source `{current_source}`. "
                    "This may indicate a failure in the primary source and should be investigated!"
                )
            else:
                payload["text"] = (
                    "⚠️ WARNING ⚠️\n\n Dead-air has been detected! "
                    "(More than 60 seconds of silence)\n\n"
                    "This automated message is for the current DJ(s). "
                    "The audio console in the studio has switched to "
                    "the backup source due to a failure. "
                    "Double-check that you are broadcasting; if in "
                    "doubt, turn the automation input on and make sure "
                    "that the volume slider is up.\n\n"
                    "If the automating input isn't working, find a "
                    "radio safe playlist or CD to loop. Please do not "
                    "leave until management is contacted."
                )
        logger.debug("Sending GroupMe payload: %s", payload)
        response = requests.post(
            config.get("GROUPME_API_BASE_URL") + "/bots/post", json=payload, timeout=5
        )
        response.raise_for_status()
        logger.debug("GroupMe message sent successfully")
    except requests.exceptions.RequestException as e:
        logger.error("Error sending GroupMe message: `%s`", e)


def main():
    """
    Monitor digital pin and send webhook on state change.
    """
    prev_state = DIGITAL_PIN.value
    prev_source = PRIMARY_SOURCE if prev_state else BACKUP_SOURCE
    logger.info(
        "%s initial state is %s (input source `%s`)", PIN_NAME, prev_state, prev_source
    )

    # Wait for the pin to change state
    while True:
        current_state = DIGITAL_PIN.value
        current_source = PRIMARY_SOURCE if current_state else BACKUP_SOURCE
        if current_state != prev_state:
            logger.info("Source changed from `%s` to `%s`", prev_source, current_source)
            persona = send_discord(current_source)
            send_groupme(current_source)

            # If we're switching to backup, attempt to send an email to
            # the DJ who is currently on air.
            if persona and persona.get("email") and current_source == BACKUP_SOURCE:
                send_email(
                    subject="ATTN: Failsafe Activated, Action Required",
                    body=(
                        "Hey! If you're getting this automated email, "
                        "it means that the audio console in the WBOR "
                        "studio has switched to the backup source due "
                        "to a failure. Please double check that you "
                        "are broadcasting something - if in doubt, "
                        "turn the automation input on!\n\n"
                        "If the automating input isn't working, find a "
                        "radio safe playlist or CD to play on loop - DO"
                        " NOT leave the station until you have reached "
                        "out to someone from management to help you! "
                        "\n\nThanks for your help keeping the stream "
                        "live, and most importantly, FCC compliant!\n\n"
                        "If you have any questions, please reach out to"
                        " management at wbor@bowdoin.edu (do not reply "
                        "to this email as it is unattended)."
                    ),
                    to=persona["email"],
                )
                # Notify MGMT that an email was sent to the DJ.
                send_discord_email_notification(persona)

            # If an email wasn't found, send_discord() already handles
            # sending a message to the DJ group.

            # Update state
            prev_state = current_state
            prev_source = current_source
        time.sleep(0.5)


if __name__ == "__main__":
    main()
