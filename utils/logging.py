"""Logging module."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from colorlog import ColoredFormatter


def configure_logging(
    logger_name: str = "wbor_failsafe_notifier",
) -> logging.Logger:
    """Set up logging with colorized output and UTC timestamps.

    Args:
        logger_name: The name of the logger to configure.

    Returns:
        The configured logger instance with colorized output and UTC timestamps.
    """
    logger = logging.getLogger(logger_name)
    if logger.hasHandlers():
        # Avoid re-adding handlers if the logger is already configured
        return logger

    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)

    class UTCTimeFormatter(ColoredFormatter):
        """Display timestamps in UTC with colorized output."""

        def formatTime(  # noqa: N802  # Must match parent class method name
            self,
            record: logging.LogRecord,
            datefmt: str | None = None,  # noqa: ARG002
        ) -> str:
            utc_dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
            # Use ISO 8601 format with UTC suffix
            return utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Define the formatter with color and PID
    formatter = UTCTimeFormatter(
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
