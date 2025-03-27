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
import time
from datetime import datetime, timezone

import board
import digitalio
import requests
from dotenv import dotenv_values

logging.basicConfig(level=logging.INFO)

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
# If BACKUP_INPUT is "A", then primary is "B"; if BACKUP_INPUT is "B", then primary is "A".
BACKUP_SOURCE = str(config.get("BACKUP_INPUT")).upper()
PRIMARY_SOURCE = "B" if BACKUP_SOURCE == "A" else "A" if BACKUP_SOURCE == "B" else None
if PRIMARY_SOURCE is None:
    raise ValueError("`BACKUP_INPUT` must be either 'A' or 'B'.")

# Colors (in decimal)
DISCORD_EMBED_ERROR_COLOR = 16711680  # Red
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


def send_discord(current_source: str):
    """
    Fire the Discord webhook with a rich embed payload.

    The embed's color and description change based on whether the state
    indicates backup.
    """
    try:
        payload = DISCORD_EMBED_PAYLOAD.copy()
        if current_source == BACKUP_SOURCE:
            payload["content"] = "@everyone"
            payload["embeds"][0]["color"] = DISCORD_EMBED_ERROR_COLOR
            payload["embeds"][0]["description"] = (
                f"⚠️ **WARNING** ⚠️ switching to backup source `{current_source}`. "
                "This may indicate a failure in the primary source and should be investigated."
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
        logging.debug("Discord message sent successfully: `%s`", response.text)
    except requests.exceptions.Timeout as e:
        logging.error("Request timed out while sending webhook: `%s`", e)
    except requests.exceptions.HTTPError as e:
        logging.error("HTTP error occurred while sending webhook: `%s`", e)
    except requests.exceptions.ConnectionError as e:
        logging.error("Connection error occurred while sending webhook: `%s`", e)
    except requests.exceptions.RequestException as e:
        logging.error("Failed to send webhook due to a network error: `%s`", e)


def send_groupme(current_source: str):
    """
    Send a message to a GroupMe group.
    """
    try:
        payload = {
            "bot_id": config.get("GROUPME_BOT_ID"),
            "text": f"Stream switched back to primary source `{current_source}`",
        }

        if current_source == BACKUP_SOURCE:
            payload["text"] = (
                f"⚠️ WARNING ⚠️ stream switching to backup source `{current_source}`. "
                "This may indicate a failure in the primary source and should be investigated!"
            )

        response = requests.post(
            config.get("GROUPME_API_BASE_URL") + "/bots/post", json=payload, timeout=5
        )
        response.raise_for_status()
        logging.debug("GroupMe message sent successfully: `%s`", response.text)
    except requests.exceptions.Timeout as e:
        logging.error("Request timed out while sending webhook: `%s`", e)
    except requests.exceptions.HTTPError as e:
        logging.error("HTTP error occurred while sending webhook: `%s`", e)
    except requests.exceptions.ConnectionError as e:
        logging.error("Connection error occurred while sending webhook: `%s`", e)
    except requests.exceptions.RequestException as e:
        logging.error("Failed to send webhook due to a network error: `%s`", e)


def main():
    """
    Monitor digital pin and send webhook on state change.

    Log the state changes and send a Discord webhook with the
    appropriate embed payload.
    """
    # Track the previous state so we only send the webhook on a state change.
    prev_state = DIGITAL_PIN.value
    prev_source = PRIMARY_SOURCE if prev_state else BACKUP_SOURCE
    logging.info(
        "%s initial state is %s (input source `%s`)", PIN_NAME, prev_state, prev_source
    )

    # Wait for the pin to change state
    while True:
        current_state = DIGITAL_PIN.value
        current_source = PRIMARY_SOURCE if current_state else BACKUP_SOURCE
        logging.debug(
            "%s state is %s (input source `%s`)",
            PIN_NAME,
            current_state,
            current_source,
        )
        if current_state != prev_state:
            logging.info(
                "Source changed from `%s` to `%s`", prev_source, current_source
            )
            send_discord(current_source)
            prev_state = current_state
            prev_source = current_source
        time.sleep(0.5)


if __name__ == "__main__":
    main()
