"""Logging module."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from colorlog import ColoredFormatter
import pytz


def configure_logging(
    logger_name: str = "wbor_failsafe_notifier",
    timezone_name: str = "America/New_York",
) -> logging.Logger:
    """Set up logging with colorized output and timestamps in specified timezone.

    Args:
        logger_name: The name of the logger to configure.
        timezone_name: The timezone to use for timestamps (e.g., 'America/New_York').
            Defaults to 'America/New_York' if not specified.

    Returns:
        The configured logger instance with colorized output and timezone-aware
        timestamps.
    """
    logger = logging.getLogger(logger_name)
    if logger.hasHandlers():
        # Avoid re-adding handlers if the logger is already configured
        return logger

    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)

    class ConfigurableTimeFormatter(ColoredFormatter):
        """Display timestamps in specified timezone with colorized output."""

        def __init__(self, timezone_name: str, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401  # ColoredFormatter requires Any for unknown parent constructor args
            super().__init__(*args, **kwargs)
            try:
                self.user_timezone = pytz.timezone(timezone_name)
            except pytz.UnknownTimeZoneError:
                # Fall back to Eastern Time if invalid timezone provided
                self.user_timezone = pytz.timezone("America/New_York")

        def formatTime(  # noqa: N802  # Must match parent class method name
            self,
            record: logging.LogRecord,
            datefmt: str | None = None,  # noqa: ARG002  # Required by parent signature
        ) -> str:
            # Convert UTC to configured timezone
            utc_dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
            local_dt = utc_dt.astimezone(self.user_timezone)
            # Use ISO 8601 format
            return local_dt.isoformat()

    # Define the formatter with color and PID
    formatter = ConfigurableTimeFormatter(
        timezone_name,
        "%(log_color)s%(asctime)s - PID %(process)d - %(name)s - %(levelname)s - "
        "%(message)s",
        log_colors={
            "DEBUG": "white",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger
