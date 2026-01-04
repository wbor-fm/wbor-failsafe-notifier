"""RabbitMQ message consumption.

The class handles the connection to a RabbitMQ server, declares a queue, and consumes
messages from that queue in a non-blocking way for integration with the main monitoring
loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

import pika
from pika.exceptions import AMQPChannelError, AMQPConnectionError
import pika.spec

if TYPE_CHECKING:
    from collections.abc import Callable


class RabbitMQConsumer:
    """RabbitMQ consumer class.

    Handles connection, channel management, queue declaration, and message consumption
    with non-blocking operation suitable for integration with main monitoring loops.
    """

    def __init__(
        self,
        amqp_url: str,
        queue_name: str,
        exchange_name: str = "",
        routing_key: str = "",
        exchange_type: str = "topic",
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Initialize the RabbitMQ consumer.

        Args:
            amqp_url: AMQP connection URL
            queue_name: Name of the queue to consume from
            exchange_name: Name of the exchange (empty for default)
            routing_key: Routing key for queue binding
            exchange_type: Type of exchange (default: topic)
            callback: Message processing callback function
        """
        self.amqp_url = amqp_url
        self.queue_name = queue_name
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.exchange_type = exchange_type
        self.callback = callback
        self._connection: pika.BlockingConnection | None = None
        self._channel: pika.channel.Channel | None = None
        self.logger = logging.getLogger(__name__ + ".RabbitMQConsumer")
        self._consuming = False
        self._consumer_thread: threading.Thread | None = None
        self._stop_consuming = threading.Event()

    def set_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Set or update the message callback function.

        Args:
            callback: Function to call when a message is received. Should accept
                a dictionary containing the parsed JSON message data.
        """
        self.callback = callback

    def _connect(self) -> None:
        """Establish a connection to the RabbitMQ server.

        Declares the specified queue and exchange if needed, and sets up
        queue binding with the routing key if both exchange and routing key
        are specified.

        Raises:
            AMQPConnectionError: If the connection to RabbitMQ fails.
            Exception: For any other unexpected errors during connection setup.
        """
        if self._connection and self._connection.is_open:
            return

        try:
            url_parts = self.amqp_url.split("@")
            log_url = url_parts[-1] if len(url_parts) > 1 else self.amqp_url
            self.logger.info(
                "Attempting to connect to RabbitMQ server at `%s`", log_url
            )

            # Configure connection parameters with heartbeat to prevent idle disconnects
            params = pika.URLParameters(self.amqp_url)
            # Set heartbeat to 300 seconds (5 minutes) to keep connection alive
            # The server's writer_idle_timeout (default 30s) closes idle connections
            params.heartbeat = 300
            # Set blocked connection timeout to handle flow control
            params.blocked_connection_timeout = 300

            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()

            # Declare exchange if specified
            if self.exchange_name:
                self._channel.exchange_declare(
                    exchange=self.exchange_name,
                    exchange_type=self.exchange_type,
                    durable=True,
                )

            # Declare queue
            self._channel.queue_declare(queue=self.queue_name, durable=True)

            # Bind queue to exchange if both are specified
            if self.exchange_name and self.routing_key:
                self._channel.queue_bind(
                    exchange=self.exchange_name,
                    queue=self.queue_name,
                    routing_key=self.routing_key,
                )

            self.logger.info(
                "Successfully connected to RabbitMQ and declared queue `%s`",
                self.queue_name,
            )

        except (AMQPConnectionError, OSError):
            self.logger.exception("Failed to connect to RabbitMQ")
            self._connection = None
            self._channel = None
            raise
        except Exception:
            self.logger.exception(
                "An unexpected error occurred during RabbitMQ connection"
            )
            self._connection = None
            self._channel = None
            raise

    def _ensure_connected(self) -> None:
        """Ensure the connection and channel are open. If not, attempt to reconnect.

        Checks connection status and re-establishes connection if needed.
        """
        if (
            not self._connection
            or self._connection.is_closed
            or not self._channel
            or self._channel.is_closed
        ):
            self.logger.warning(
                "RabbitMQ connection/channel is closed or not established. "
                "Reconnecting..."
            )
            self._connect()

    def _message_callback(
        self,
        channel: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        properties: pika.spec.BasicProperties,  # noqa: ARG002  # Required by pika callback signature
        body: bytes,
    ) -> None:
        """Internal callback that processes incoming messages.

        Args:
            channel: The pika channel object.
            method: Message delivery method with routing information.
            properties: Message properties (unused but required by pika).
            body: Raw message body as bytes.
        """
        try:
            message_data = json.loads(body.decode("utf-8"))
            self.logger.info(
                "Received message from queue `%s`: %s", self.queue_name, message_data
            )

            if self.callback:
                self.callback(message_data)

            # Acknowledge the message
            channel.basic_ack(delivery_tag=method.delivery_tag)

        except json.JSONDecodeError:
            self.logger.exception(
                "Failed to decode JSON message from queue `%s`. Message body: %s",
                self.queue_name,
                body,
            )
            # Reject and don't requeue malformed messages
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        except Exception:
            self.logger.exception(
                "Error processing message from queue `%s`", self.queue_name
            )
            # Acknowledge to prevent reprocessing of problematic messages
            channel.basic_ack(delivery_tag=method.delivery_tag)

    def _consume_loop(self) -> None:
        """Main consumption loop that runs in a separate thread.

        Continuously processes messages until stop_consuming is set.
        Handles reconnection and error recovery automatically.
        """
        while not self._stop_consuming.is_set():
            try:
                self._ensure_connected()
                if not self._channel:
                    self.logger.error(
                        "Cannot consume, RabbitMQ channel is not available."
                    )
                    time.sleep(5)
                    continue

                # Set up the consumer
                self._channel.basic_consume(
                    queue=self.queue_name,
                    on_message_callback=self._message_callback,
                )

                self.logger.info(
                    "Starting to consume messages from queue `%s`", self.queue_name
                )
                self._consuming = True

                # Process messages with timeout to allow for periodic checks
                while not self._stop_consuming.is_set() and self._consuming:
                    try:
                        self._connection.process_data_events(time_limit=1.0)  # type: ignore[union-attr]
                    except Exception as e:
                        self.logger.warning("Error processing data events: %s", e)
                        break

            except (AMQPConnectionError, AMQPChannelError, OSError):
                self.logger.exception("Connection/Channel error during consumption")
                self._consuming = False
                time.sleep(5)
            except Exception:
                self.logger.exception("Unexpected error in consume loop")
                self._consuming = False
                time.sleep(5)

        self.logger.info("Consumer loop stopped")

    def start_consuming(self) -> bool:
        """Start consuming messages in a separate thread.

        Returns:
        - bool: True if consumption started successfully, False otherwise
        """
        # Start the thread even if we currently can't connect; the loop will retry.
        if self._consuming:
            self.logger.warning("Consumer is already running")
            return True
        self._stop_consuming.clear()
        self._consumer_thread = threading.Thread(target=self._consume_loop, daemon=True)
        self._consumer_thread.start()
        time.sleep(0.5)
        return self._consumer_thread.is_alive()

    def stop_consuming(self) -> None:
        """Stop consuming messages and close connections.

        Signals the consumer thread to stop and waits for it to complete.
        """
        self.logger.info("Stopping RabbitMQ consumer...")
        self._stop_consuming.set()
        self._consuming = False

        if self._consumer_thread and self._consumer_thread.is_alive():
            self._consumer_thread.join(timeout=10)
            if self._consumer_thread.is_alive():
                self.logger.warning("Consumer thread did not stop within timeout")

        self.close()

    def close(self) -> None:
        """Close the RabbitMQ connection and channel.

        Safely closes all RabbitMQ resources to prevent connection leaks.
        """
        closed_something = False
        try:
            if self._channel and self._channel.is_open:
                self._channel.close()
                self.logger.info("RabbitMQ channel closed.")
                closed_something = True
        except Exception:
            self.logger.exception("Error closing RabbitMQ channel")

        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
                self.logger.info("RabbitMQ connection closed.")
                closed_something = True
        except Exception:
            self.logger.exception("Error closing RabbitMQ connection")

        if not closed_something:
            self.logger.info(
                "RabbitMQ connection/channel already closed or not established."
            )

        self._channel = None
        self._connection = None

    def check_single_message(self) -> dict[str, Any] | None:
        """Check for a single message without starting continuous consumption.

        Useful for polling-style message checking.

        Returns:
        - dict or None: Message data if available, None otherwise
        """
        try:
            self._ensure_connected()
            if not self._channel:
                return None

            method_frame, header_frame, body = self._channel.basic_get(
                queue=self.queue_name, auto_ack=True
            )

            if method_frame:
                try:
                    message_data = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    self.logger.exception(
                        "Failed to decode JSON message. Message body: %s", body
                    )
                    return None
                else:
                    self.logger.info(
                        "Retrieved single message from queue `%s`: %s",
                        self.queue_name,
                        message_data,
                    )
                    return message_data  # type: ignore[no-any-return]
            else:
                return None

        except Exception:
            self.logger.exception("Error checking for single message")
            return None
