"""RabbitMQPublisher class for RabbitMQ message publishing.

The class handles the connection to a RabbitMQ server, declares an exchange, and
publishes messages to that exchange with retry logic and publisher confirms.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pika
from pika.exceptions import (
    AMQPChannelError,
    AMQPConnectionError,
    AMQPError,
    StreamLostError,
    UnroutableError,
)


class RabbitMQPublisher:
    """RabbitMQ publisher class.

    Handles connection, channel management, exchange declaration, and message publishing
    with retries and publisher confirms.
    """

    def __init__(
        self, amqp_url: str, exchange_name: str, exchange_type: str = "topic"
    ) -> None:
        """Initialize the RabbitMQ publisher.

        Args:
            amqp_url: AMQP connection URL
            exchange_name: Name of the exchange to publish to
            exchange_type: Type of exchange (default: topic)
        """
        self.amqp_url = amqp_url
        self.exchange_name = exchange_name
        self.exchange_type = exchange_type
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.channel.Channel | None = None
        self.logger = logging.getLogger(__name__ + ".RabbitMQPublisher")
        self._connect()

    def _connect(self, *, is_reconnect: bool = False) -> None:
        """Establish a connection to the RabbitMQ server and declare the exchange.

        Called during initialization and whenever a connection is needed.

        Args:
            is_reconnect: Whether this is a reconnection attempt (adds stabilization).
        """
        if self._connection and self._connection.is_open:
            # Verify the channel is also healthy
            if self._channel and self._channel.is_open:
                return
            # Connection open but channel closed - need to recreate channel
            self.logger.warning(
                "Connection open but channel closed, recreating channel"
            )

        try:
            # Clean up any stale connection/channel state first
            self._cleanup_connection()

            # Log only the host part of the URL to avoid credential leak
            url_parts = self.amqp_url.split("@")
            log_url = url_parts[-1] if len(url_parts) > 1 else self.amqp_url
            self.logger.info(
                "%s RabbitMQ server at `%s`",
                "Reconnecting to" if is_reconnect else "Connecting to",
                log_url,
            )

            # Configure connection parameters with heartbeat to prevent idle disconnects
            params = pika.URLParameters(self.amqp_url)
            # Set heartbeat to 60 seconds to detect dead connections faster
            # This sends heartbeats every 60s and expects response within 60s
            params.heartbeat = 60
            # Set blocked connection timeout to handle flow control
            params.blocked_connection_timeout = 300
            # Set socket timeout to prevent hanging on network issues
            params.socket_timeout = 30

            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()
            self._channel.exchange_declare(
                exchange=self.exchange_name,
                exchange_type=self.exchange_type,
                durable=True,
            )
            self._channel.confirm_delivery()

            # Process any pending I/O events to ensure channel is fully ready
            # This helps prevent race conditions after reconnection
            if self._connection:
                self._connection.process_data_events(time_limit=0)

            # Add stabilization delay after reconnect to let the channel settle
            if is_reconnect:
                stabilization_delay = 1.0  # 1 second
                self.logger.info(
                    "Waiting %.1fs for connection to stabilize...", stabilization_delay
                )
                time.sleep(stabilization_delay)
                # Process events again after delay
                if self._connection and self._connection.is_open:
                    self._connection.process_data_events(time_limit=0)

            self.logger.info(
                "Successfully %s RabbitMQ and declared exchange `%s` (type: %s)",
                "reconnected to" if is_reconnect else "connected to",
                self.exchange_name,
                self.exchange_type,
            )
        except (
            AMQPConnectionError,
            AMQPError,
            OSError,
        ) as e:
            self.logger.exception(
                "Failed to connect to RabbitMQ (%s)", type(e).__name__
            )
            self._connection = None
            self._channel = None
            raise

    def _cleanup_connection(self) -> None:
        """Clean up any existing connection/channel resources.

        Called before reconnecting to ensure clean state.
        """
        try:
            if self._channel:
                try:
                    if self._channel.is_open:
                        self._channel.close()
                except Exception:
                    self.logger.debug(
                        "Error closing channel during cleanup", exc_info=True
                    )
                self._channel = None
        except Exception:
            self._channel = None

        try:
            if self._connection:
                try:
                    if self._connection.is_open:
                        self._connection.close()
                except Exception:
                    self.logger.debug(
                        "Error closing connection during cleanup", exc_info=True
                    )
                self._connection = None
        except Exception:
            self._connection = None

    def _ensure_connected(self) -> None:
        """Ensure the connection and channel are open.

        If not, it attempts to reconnect. This is useful for checking the state of the
        connection before publishing a message.
        """
        needs_reconnect = False
        reason = ""

        if not self._connection:
            needs_reconnect = True
            reason = "no connection object"
        elif self._connection.is_closed:
            needs_reconnect = True
            reason = "connection is closed"
        elif not self._channel:
            needs_reconnect = True
            reason = "no channel object"
        elif self._channel.is_closed:
            needs_reconnect = True
            reason = "channel is closed"

        if needs_reconnect:
            self.logger.warning(
                "RabbitMQ connection/channel unavailable (%s). Reconnecting...", reason
            )
            self._connect(is_reconnect=True)

    def publish(
        self,
        routing_key: str,
        message_body: dict[str, Any],
        retry_attempts: int = 3,
        retry_delay_seconds: int = 5,
    ) -> bool:
        """Publish a message to the RabbitMQ exchange with the specified routing key.

        Uses exponential backoff for retrying on connection or channel errors.

        Parameters:
        - routing_key (str): The routing key for the message.
        - message_body (Dict[str, Any]): The message body to publish.
        - retry_attempts (int): Number of retry attempts on failure.
        - retry_delay_seconds (int): Delay between retry attempts in
            seconds.

        Returns:
        - bool: True if the message was published successfully, False
            otherwise.
        """
        try:
            self._ensure_connected()
        except (AMQPConnectionError, AMQPChannelError, StreamLostError, OSError):
            self.logger.exception(
                "Failed to ensure RabbitMQ connection before publishing"
            )
            return False

        if not self._channel:
            self.logger.error("Cannot publish, RabbitMQ channel is not available.")
            return False

        try:
            message_body_str = json.dumps(message_body)
        except TypeError:
            self.logger.exception(
                "Failed to serialize message body to JSON. Message: %s",
                message_body,
            )
            return False

        for attempt in range(retry_attempts):
            try:
                # Process any pending I/O events before publishing to catch stale
                # connections. This helps detect if the connection died since we last
                # checked.
                if self._connection and self._connection.is_open:
                    self._connection.process_data_events(time_limit=0)

                # Re-verify connection is still good after processing events
                if (
                    not self._connection
                    or self._connection.is_closed
                    or not self._channel
                    or self._channel.is_closed
                ):
                    msg = "Connection lost after processing events"
                    raise StreamLostError(msg)  # noqa: TRY301

                # Pika's BlockingChannel.basic_publish with confirms enabled returns
                # True on ACK, False/None on NACK/timeout However, behavior can be
                # subtle. Checking for exceptions is more robust for BlockingConnection.
                # If basic_publish itself raises an exception (e.g., channel closed),
                # it's a clear failure. If mandatory=True and message is unroutable,
                # UnroutableError is raised.

                self._channel.basic_publish(
                    exchange=self.exchange_name,
                    routing_key=routing_key,
                    body=message_body_str,
                    properties=pika.BasicProperties(
                        # Make message persistent
                        delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE,
                        content_type="application/json",
                    ),
                    mandatory=True,  # Helps detect unroutable messages
                )

                # Process events after publish to ensure delivery confirmation
                if self._connection and self._connection.is_open:
                    self._connection.process_data_events(time_limit=0)

            except UnroutableError:
                self.logger.exception(
                    "Message to exchange `%s` with routing key `%s` was unroutable. "
                    "Ensure a queue is bound correctly or the exchange exists.",
                    self.exchange_name,
                    routing_key,
                )
                # Unroutable messages, don't retry without a configuration change
                return False
            except (
                StreamLostError,
                AMQPConnectionError,
                AMQPChannelError,
                OSError,
            ) as e:
                error_type = type(e).__name__
                self.logger.warning(
                    "%s during publish (attempt %d/%d): %s",
                    error_type,
                    attempt + 1,
                    retry_attempts,
                    e,
                )
                if attempt < retry_attempts - 1:
                    delay = retry_delay_seconds * (attempt + 1)  # Exponential backoff
                    self.logger.info("Retrying publish in %d seconds...", delay)
                    time.sleep(delay)
                    try:
                        # Force reconnection with stabilization delay
                        self._connect(is_reconnect=True)
                    except (AMQPConnectionError, AMQPChannelError, StreamLostError):
                        self.logger.exception(
                            "Failed to reconnect during retry attempt %d", attempt + 1
                        )
                        # Continue to next retry attempt if any, or fail
                else:
                    # Using .error() - exception details already logged above
                    self.logger.error(  # noqa: TRY400
                        "Failed to publish message after %d attempts due to %s.",
                        retry_attempts,
                        error_type,
                    )
                    return False
            except Exception:
                self.logger.exception(
                    "Unexpected error during publish (attempt %d/%d)",
                    attempt + 1,
                    retry_attempts,
                )
                if attempt >= retry_attempts - 1:
                    # Using .error() - exception logged above with .exception()
                    self.logger.error(  # noqa: TRY400
                        "Failed to publish message to exchange `%s` with routing key "
                        "`%s` after %d attempts.",
                        self.exchange_name,
                        routing_key,
                        retry_attempts,
                    )
                    return False
                # Try reconnecting for unexpected errors too
                delay = retry_delay_seconds * (attempt + 1)
                time.sleep(delay)
                try:
                    self._connect(is_reconnect=True)
                except Exception:
                    self.logger.exception("Failed to reconnect after unexpected error")
            else:
                # Success if no exception raised with confirm_delivery enabled
                self.logger.info(
                    "Successfully published message to exchange `%s` with routing key"
                    " `%s` (attempt %d)",
                    self.exchange_name,
                    routing_key,
                    attempt + 1,
                )
                return True

        self.logger.error(
            "Failed to publish message to exchange `%s` with routing key "
            "`%s` after %d attempts.",
            self.exchange_name,
            routing_key,
            retry_attempts,
        )
        return False

    def close(self) -> None:
        """Closes a RabbitMQ connection and channel if they are open.

        This method should be called when the publisher is no longer needed to ensure
        proper resource cleanup.
        """
        closed_something = False
        try:
            if self._channel and self._channel.is_open:
                self._channel.close()
                self.logger.info("RabbitMQ channel closed.")
                closed_something = True
        except Exception:  # Need to handle any exception during cleanup
            self.logger.exception("Error closing RabbitMQ channel")
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
                self.logger.info("RabbitMQ connection closed.")
                closed_something = True
        except Exception:  # Need to handle any exception during cleanup
            self.logger.exception("Error closing RabbitMQ connection")

        if not closed_something:
            self.logger.info(
                "RabbitMQ connection/channel already closed or not established."
            )

        self._channel = None
        self._connection = None
