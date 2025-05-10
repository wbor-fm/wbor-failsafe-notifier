"""
This module provides a RabbitMQPublisher class that handles the
connection to a RabbitMQ server, declares an exchange, and publishes
messages to that exchange with retry logic and publisher confirms.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

import pika
from pika.exceptions import AMQPChannelError, AMQPConnectionError, UnroutableError


class RabbitMQPublisher:
    """
    A RabbitMQ publisher class that handles connection, channel
    management, exchange declaration, and message publishing with
    retries and publisher confirms.
    """

    def __init__(self, amqp_url: str, exchange_name: str, exchange_type: str = "topic"):
        self.amqp_url = amqp_url
        self.exchange_name = exchange_name
        self.exchange_type = exchange_type
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[pika.channel.Channel] = None
        self.logger = logging.getLogger(__name__ + ".RabbitMQPublisher")
        self._connect()

    def _connect(self) -> None:
        """
        Establish a connection to the RabbitMQ server and declare the
        exchange. Called during initialization and whenever a connection
        is needed.
        """
        if self._connection and self._connection.is_open:
            return
        try:
            # Log only the host part of the URL to avoid credential leak
            url_parts = self.amqp_url.split("@")
            log_url = url_parts[-1] if len(url_parts) > 1 else self.amqp_url
            self.logger.info(
                "Attempting to connect to RabbitMQ server at `%s`", log_url
            )

            self._connection = pika.BlockingConnection(
                pika.URLParameters(self.amqp_url)
            )
            self._channel = self._connection.channel()
            self._channel.exchange_declare(
                exchange=self.exchange_name,
                exchange_type=self.exchange_type,
                durable=True,
            )
            self._channel.confirm_delivery()
            self.logger.info(
                "Successfully connected to RabbitMQ and declared exchange `%s` (type: %s)",
                self.exchange_name,
                self.exchange_type,
            )
        except AMQPConnectionError as e:
            self.logger.error("Failed to connect to RabbitMQ: %s", e)
            self._connection = None
            self._channel = None
            raise
        except (
            Exception
        ) as e:  # Catch other potential pika or general errors during setup
            self.logger.error(
                "An unexpected error occurred during RabbitMQ connection: %s", e
            )
            self._connection = None
            self._channel = None
            raise

    def _ensure_connected(self) -> None:
        """
        Method to ensure the connection and channel are open. If not,
        it attempts to reconnect. This is useful for checking the state
        of the connection before publishing a message.
        """
        if (
            not self._connection
            or self._connection.is_closed
            or not self._channel
            or self._channel.is_closed
        ):
            self.logger.warning(
                "RabbitMQ connection/channel is closed or not established. Reconnecting..."
            )
            self._connect()

    def publish(
        self,
        routing_key: str,
        message_body: Dict[str, Any],
        retry_attempts: int = 3,
        retry_delay_seconds: int = 5,
    ) -> bool:
        """
        Publish a message to the RabbitMQ exchange with the specified
        routing key. Uses exponential backoff for retrying on connection
        or channel errors.

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
        except Exception as e:  # pylint: disable=broad-except
            self.logger.error(
                "Failed to ensure RabbitMQ connection before publishing: %s", e
            )
            return False  # Cannot publish if connection cannot be ensured

        if not self._channel:
            self.logger.error("Cannot publish, RabbitMQ channel is not available.")
            return False

        try:
            message_body_str = json.dumps(message_body)
        except TypeError as e:
            self.logger.error(
                "Failed to serialize message body to JSON: `%s`. Message: %s",
                e,
                message_body,
            )
            return False

        for attempt in range(retry_attempts):
            try:
                # Pika's BlockingChannel.basic_publish with confirms enabled returns True on ACK,
                # False/None on NACK/timeout However, behavior can be subtle. Checking for
                # exceptions is more robust for BlockingConnection. If basic_publish itself raises
                # an exception (e.g., channel closed), it's a clear failure. If mandatory=True and
                # message is unroutable, UnroutableError is raised.

                self._channel.basic_publish(
                    exchange=self.exchange_name,
                    routing_key=routing_key,
                    body=message_body_str,
                    properties=pika.BasicProperties(
                        delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE,  # Make message persistent
                        content_type="application/json",
                    ),
                    mandatory=True,  # Helps detect unroutable messages
                )

                # Assuming success if no exception is raised with confirm_delivery enabled
                self.logger.info(
                    "Successfully published message to exchange `%s` with routing key `%s` "
                    "(attempt %d)",
                    self.exchange_name,
                    routing_key,
                    attempt + 1,
                )
                return True

            except UnroutableError:
                self.logger.error(
                    "Message to exchange `%s` with routing key `%s` was unroutable. "
                    "Ensure a queue is bound correctly or the exchange exists.",
                    self.exchange_name,
                    routing_key,
                )
                return False  # Unroutable messages, don't retry without a configuration change
            except (AMQPConnectionError, AMQPChannelError) as e:
                self.logger.error(
                    "Connection/Channel error during publish (attempt %d/%d): %s",
                    attempt + 1,
                    retry_attempts,
                    e,
                )
                if attempt < retry_attempts - 1:
                    self.logger.info(
                        "Retrying publish in %d seconds...", retry_delay_seconds
                    )
                    time.sleep(
                        retry_delay_seconds * (attempt + 1)
                    )  # Basic exponential backoff
                    try:
                        self._connect()  # Attempt to re-establish connection before next retry
                    except Exception as recon_e:  # pylint: disable=broad-except
                        self.logger.error(
                            "Failed to reconnect during retry attempt: %s", recon_e
                        )
                        # Continue to next retry attempt if any, or fail
                else:
                    self.logger.error(
                        "Failed to publish message after %d attempts due to connection/channel "
                        "issues.",
                        retry_attempts,
                    )
                    return False
            except Exception as e:  # pylint: disable=broad-except
                self.logger.error(
                    "An unexpected error occurred during publish (attempt %d/%d): %s",
                    attempt + 1,
                    retry_attempts,
                    e,
                )
                self.logger.error(
                    "Failed to publish message to exchange `%s` with routing key `%s` after %d "
                    "attempts.",
                    self.exchange_name,
                    routing_key,
                    retry_attempts,
                )
                # Fall through to retry or fail after attempts

            if attempt >= retry_attempts - 1:  # If loop finishes without returning True
                self.logger.error(
                    "Failed to publish message to exchange `%s` with routing key `%s` after %d "
                    "attempts.",
                    self.exchange_name,
                    routing_key,
                    retry_attempts,
                )
                return False
        return False  # Should be unreachable if loop logic is correct

    def close(self) -> None:
        """
        Closes a RabbitMQ connection and channel if they are open. This
        method should be called when the publisher is no longer needed
        to ensure proper resource cleanup.
        """
        closed_something = False
        try:
            if self._channel and self._channel.is_open:
                self._channel.close()
                self.logger.info("RabbitMQ channel closed.")
                closed_something = True
        except Exception as e:  # pylint: disable=broad-except
            self.logger.error("Error closing RabbitMQ channel: %s", e)
        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
                self.logger.info("RabbitMQ connection closed.")
                closed_something = True
        except Exception as e:  # pylint: disable=broad-except
            self.logger.error("Error closing RabbitMQ connection: %s", e)

        if not closed_something:
            self.logger.info(
                "RabbitMQ connection/channel already closed or not established."
            )

        self._channel = None
        self._connection = None
