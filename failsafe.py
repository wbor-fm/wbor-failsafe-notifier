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
    except smtplib.SMTPDataError as e:
        logger.error("SMTP data error: `%s`", e)
    except smtplib.SMTPConnectError as e:
        logger.error("Failed to connect to SMTP server: `%s`", e)
    except smtplib.SMTPAuthenticationError as e:
        logger.error("SMTP authentication error: `%s`", e)
    except smtplib.SMTPHeloError as e:
        logger.error("SMTP HELO error: `%s`", e)
    except smtplib.SMTPServerDisconnected as e:
        logger.error("SMTP server disconnected: `%s`", e)
    except smtplib.SMTPException as e:
        logger.error("SMTP error occurred: `%s`", e)
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Failed to send email: `%s`", e)


def get_current_playlist() -> dict:
    """
    Get the current playlist from Spinitron API.
    """
    logger.debug("Fetching current playlist from Spinitron API...")
    try:
        response = requests.get(
            f"{config.get('SPINITRON_API_BASE_URL')}/playlists", timeout=5
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("items", [])
        if items:
            playlist = items[0]
            logger.debug("Current playlist: `%s`", playlist)
            return playlist
        logger.error("No playlist items found in the response: `%s`", data)
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error("Connection error occurred while fetching playlist: `%s`", e)
        return None
    except requests.exceptions.Timeout as e:
        logger.error("Request timed out while fetching playlist: `%s`", e)
        return None
    except requests.exceptions.TooManyRedirects as e:
        logger.error("Too many redirects while fetching playlist: `%s`", e)
        return None
    except requests.exceptions.InvalidURL as e:
        logger.error("Invalid URL while fetching playlist: `%s`", e)
        return None
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error occurred while fetching playlist: `%s`", e)
        return None
    except requests.exceptions.RequestException as e:
        logger.error("Failed to fetch current playlist: `%s`", e)
        return None
    except IndexError:
        logger.error("No playlists found in the response.")
        return None
    except ValueError:
        logger.error("Failed to parse JSON response.")
        return None
    except Exception as e:  # pylint: disable=broad-except
        logger.error(
            "An unexpected error occurred while getting the current playlist: `%s`", e
        )
    return None


def get_persona(persona_id: int) -> dict:
    """
    Get the persona from Spinitron API.
    """
    logger.debug("Fetching persona from Spinitron API...")
    try:
        response = requests.get(
            f"{config.get('SPINITRON_API_BASE_URL')}/personas/{persona_id}", timeout=5
        )
        response.raise_for_status()
        if response.json():
            logger.debug("Persona: `%s`", response.json())
            return response.json()
        logger.error("No persona found in the response.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error("Failed to fetch persona: `%s`", e)
        return None
    except ValueError:
        logger.error("Failed to parse JSON response.")
        return None
    except Exception as e:  # pylint: disable=broad-except
        logger.error(
            "An unexpected error occurred while getting the current persona: `%s`", e
        )
    return None


import json
from datetime import datetime, timezone


def send_discord_email_notification(persona: dict) -> None:
    """
    Fire the Discord webhook with a rich embed payload notifying MGMT
    that an email was sent to the DJ.
    """
    try:
        payload = DISCORD_EMBED_PAYLOAD.copy()
        fields = []

        if persona.get("string"):
            fields.append(
                {
                    "name": "DJ",
                    "value": persona["string"],
                }
            )

        if persona.get("playlist"):
            playlist = persona["playlist"]
            fields.append(
                {
                    "name": "Playlist",
                    "value": (
                        f"[{playlist['title']}](https://api-1.wbor.org"
                        f"/api/playlists/{playlist['id']})"
                    ),
                }
            )

        payload["embeds"][0]["title"] = "Failsafe Gadget - Email Sent"
        payload["embeds"][0]["color"] = DISCORD_EMBED_WARNING_COLOR
        payload["embeds"][0]["description"] = (
            f"Email sent to `{persona['email']}` regarding backup "
            "source activation. Please check if the DJ is aware of the"
            " backup source activation and if they need assistance."
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
    except requests.exceptions.Timeout as e:
        logger.error("Request timed out while sending Discord email webhook: `%s`", e)
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error occurred while sending Discord email webhook: `%s`", e)
    except requests.exceptions.ConnectionError as e:
        logger.error(
            "Connection error occurred while sending Discord email webhook: `%s`", e
        )
    except requests.exceptions.RequestException as e:
        logger.error(
            "Failed to send Discord email webhook due to a network error: `%s`", e
        )


def send_discord(current_source: str) -> dict:
    """
    Fire the Discord webhook with a rich embed payload.

    The embed's color and description change based on whether the state
    indicates backup.

    Returns dict with info about the current playlist and DJ.
    """
    try:
        payload = DISCORD_EMBED_PAYLOAD.copy()
        fields = []

        playlist = get_current_playlist()

        persona = None
        persona_name = None
        persona_email = None
        persona_str = None

        if playlist:
            # Convert the start and end times to a more readable format
            start_time = datetime.fromisoformat(playlist["start"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            end_time = datetime.fromisoformat(playlist["end"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            is_automation = playlist.get("automation") == "1"

            logger.debug("Playlist: `%s`", playlist)
            logger.debug("Is automation: `%s`", is_automation)

            if not is_automation:
                persona = get_persona(playlist["persona_id"])
                if not persona:
                    logger.error("Failed to fetch persona for playlist: `%s`", playlist)
                persona_name = persona["name"] if persona else "Unknown"
                persona_email = persona["email"] if persona else None

                persona_str = None
                if persona_email:
                    persona_str = f"[{persona_name}](mailto:{persona_email})"
                else:
                    persona_str = persona_name

                    # TODO:
                    # At this point, we should see if the show has any
                    # other personas associated with it and attempt to
                    # get their email address instead.

                    # If we can't find one, we should send a message to
                    # the DJ-wide GroupMe group.
                    send_groupme(
                        current_source,
                        public=True,
                        bot_id=config.get("GROUPME_BOT_ID_DJ"),
                    )

            fields.extend(
                [
                    {
                        "name": "Playlist",
                        "value": (
                            f"[{playlist['title']}](https://api-1.wbor"
                            f".org/api/playlists/{playlist['id']})"
                        ),
                    },
                    {
                        "name": "DJ",
                        "value": persona_str,
                    },
                    {
                        "name": "Start",
                        "value": start_time,
                        "inline": True,
                    },
                    {
                        "name": "End",
                        "value": end_time,
                        "inline": True,
                    },
                ]
            )

        if current_source == BACKUP_SOURCE:
            payload["content"] = "@everyone - stream may be down!"
            payload["embeds"][0]["color"] = DISCORD_EMBED_ERROR_COLOR
            payload["embeds"][0]["description"] = (
                f"⚠️ **WARNING** ⚠️ switching to backup source `{current_source}`. "
                "This may indicate a failure in the primary source and should be investigated."
                "\n\n"
                "Information about the current playlist is below. "
            )
            if fields:
                payload["embeds"][0]["fields"] = fields
                logger.debug("send_discord() Fields: `%s`", fields)
            else:
                payload["embeds"][0]["description"] += (
                    "\n\nNo playlist information available. Please "
                    "check the Spinitron API for more details."
                )
                logger.warning("No Spinitron fields found!")
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
    except requests.exceptions.Timeout as e:
        logger.error("Request timed out while sending webhook: `%s`", e)
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error occurred while sending webhook: `%s`", e)
    except requests.exceptions.ConnectionError as e:
        logger.error("Connection error occurred while sending webhook: `%s`", e)
    except requests.exceptions.RequestException as e:
        logger.error("Failed to send webhook due to a network error: `%s`", e)
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

        if current_source == BACKUP_SOURCE and not public:
            payload["text"] = (
                f"⚠️ WARNING ⚠️ stream switching to backup source `{current_source}`. "
                "This may indicate a failure in the primary source and should be investigated!"
            )
        elif current_source == BACKUP_SOURCE and public:
            # Message for the DJ group (`public`)
            payload["text"] = (
                "⚠️ WARNING ⚠️ Dead-air has been detected!\n\n"
                "This is an automated message meant for the current "
                "DJ(s). The audio console in the studio has switched to"
                " the backup source due to a failure. Please double "
                "check that you are broadcasting something - if in "
                "doubt, turn the automation input on!\n\n"
                "If the automating input isn't working, find a "
                "radio safe playlist or CD to play on loop - DO"
                " NOT leave the station until you have reached "
                "out to someone from management to help you! "
                "\n\nThanks for your help keeping the stream "
                "live, and most importantly, FCC compliant!\n\n"
                "If you have any questions, please reach out to"
                " management at wbor@bowdoin.edu"
            )

        response = requests.post(
            config.get("GROUPME_API_BASE_URL") + "/bots/post", json=payload, timeout=5
        )
        response.raise_for_status()
        logger.debug("GroupMe message sent successfully")
    except requests.exceptions.Timeout as e:
        logger.error("Request timed out while sending webhook: `%s`", e)
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error occurred while sending webhook: `%s`", e)
    except requests.exceptions.ConnectionError as e:
        logger.error("Connection error occurred while sending webhook: `%s`", e)
    except requests.exceptions.RequestException as e:
        logger.error("Failed to send webhook due to a network error: `%s`", e)


def main():
    """
    Monitor digital pin and send webhook on state change.

    Log the state changes and send a Discord webhook with the
    appropriate embed payload.
    """
    # Track the previous state so we only send the webhook on a state
    # change.
    prev_state = DIGITAL_PIN.value
    prev_source = PRIMARY_SOURCE if prev_state else BACKUP_SOURCE
    logger.info(
        "%s initial state is %s (input source `%s`)", PIN_NAME, prev_state, prev_source
    )

    # Wait for the pin to change state
    while True:
        current_state = DIGITAL_PIN.value
        current_source = PRIMARY_SOURCE if current_state else BACKUP_SOURCE
        # logger.debug(
        #     "%s state is %s (input source `%s`)",
        #     PIN_NAME,
        #     current_state,
        #     current_source,
        # )
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

            # If an email wasn't found, send_discord already handles
            # sending a message to the DJ group.

            # Update state
            prev_state = current_state
            prev_source = current_source
        time.sleep(0.5)


if __name__ == "__main__":
    main()
