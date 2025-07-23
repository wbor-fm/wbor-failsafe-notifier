"""
This module provides a RabbitMQConsumer class that handles the connection to a
RabbitMQ server, declares a queue, and consumes messages from that queue in a
non-blocking way for integration with the main monitoring loop.
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

import pika
from pika.exceptions import AMQPChannelError, AMQPConnectionError


class RabbitMQConsumer:
    """
    A RabbitMQ consumer class that handles connection, channel management, queue 
    declaration, and message consumption with non-blocking operation suitable
    for integration with main monitoring loops.
    """

    def __init__(
        self,
        amqp_url: str,
        queue_name: str,
        exchange_name: str = "",
        routing_key: str = "",
        exchange_type: str = "topic",
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.amqp_url = amqp_url
        self.queue_name = queue_name
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.exchange_type = exchange_type
        self.callback = callback
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[pika.channel.Channel] = None
        self.logger = logging.getLogger(__name__ + ".RabbitMQConsumer")
        self._consuming = False
        self._consumer_thread: Optional[threading.Thread] = None
        self._stop_consuming = threading.Event()

    def set_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Set or update the message callback function.
        
        Parameters:
        - callback: Function to call when a message is received
        """
        self.callback = callback

    def _connect(self) -> None:
        """
        Establish a connection to the RabbitMQ server and declare the queue and
        exchange if needed.
        """
        if self._connection and self._connection.is_open:
            return

        try:
            url_parts = self.amqp_url.split("@")
            log_url = url_parts[-1] if len(url_parts) > 1 else self.amqp_url
            self.logger.info(
                "Attempting to connect to RabbitMQ server at `%s`", log_url
            )

            self._connection = pika.BlockingConnection(
                pika.URLParameters(self.amqp_url)
            )
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

        except AMQPConnectionError as e:
            self.logger.error("Failed to connect to RabbitMQ: %s", e)
            self._connection = None
            self._channel = None
            raise
        except Exception as e:
            self.logger.error(
                "An unexpected error occurred during RabbitMQ connection: %s", e
            )
            self._connection = None
            self._channel = None
            raise

    def _ensure_connected(self) -> None:
        """
        Ensure the connection and channel are open. If not, attempt to reconnect.
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

    def _message_callback(self, channel, method, properties, body):
        """
        Internal callback that processes incoming messages.
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

        except json.JSONDecodeError as e:
            self.logger.error(
                "Failed to decode JSON message from queue `%s`: %s. Message body: %s",
                self.queue_name,
                e,
                body,
            )
            # Reject and don't requeue malformed messages
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        except Exception as e:
            self.logger.error(
                "Error processing message from queue `%s`: %s", self.queue_name, e
            )
            # Acknowledge to prevent reprocessing of problematic messages
            channel.basic_ack(delivery_tag=method.delivery_tag)

    def _consume_loop(self) -> None:
        """
        Main consumption loop that runs in a separate thread.
        """
        while not self._stop_consuming.is_set():
            try:
                self._ensure_connected()
                if not self._channel:
                    self.logger.error("Cannot consume, RabbitMQ channel is not available.")
                    time.sleep(5)
                    continue

                # Set up the consumer
                self._channel.basic_consume(
                    queue=self.queue_name,
                    on_message_callback=self._message_callback,
                )

                self.logger.info("Starting to consume messages from queue `%s`", self.queue_name)
                self._consuming = True

                # Process messages with timeout to allow for periodic checks
                while not self._stop_consuming.is_set() and self._consuming:
                    try:
                        self._connection.process_data_events(time_limit=1.0)
                    except Exception as e:
                        self.logger.warning("Error processing data events: %s", e)
                        break

            except (AMQPConnectionError, AMQPChannelError) as e:
                self.logger.error("Connection/Channel error during consumption: %s", e)
                self._consuming = False
                time.sleep(5)  # Wait before retry
            except Exception as e:
                self.logger.error("Unexpected error in consume loop: %s", e)
                self._consuming = False
                time.sleep(5)

        self.logger.info("Consumer loop stopped")

    def start_consuming(self) -> bool:
        """
        Start consuming messages in a separate thread.
        
        Returns:
        - bool: True if consumption started successfully, False otherwise
        """
        if self._consuming:
            self.logger.warning("Consumer is already running")
            return True

        try:
            self._ensure_connected()
        except Exception as e:
            self.logger.error("Failed to connect before starting consumer: %s", e)
            return False

        self._stop_consuming.clear()
        self._consumer_thread = threading.Thread(target=self._consume_loop, daemon=True)
        self._consumer_thread.start()

        # Wait a moment to see if the thread starts successfully
        time.sleep(0.5)
        return self._consumer_thread.is_alive()

    def stop_consuming(self) -> None:
        """
        Stop consuming messages and close connections.
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
        """
        Close the RabbitMQ connection and channel.
        """
        closed_something = False
        try:
            if self._channel and self._channel.is_open:
                self._channel.close()
                self.logger.info("RabbitMQ channel closed.")
                closed_something = True
        except Exception as e:
            self.logger.error("Error closing RabbitMQ channel: %s", e)

        try:
            if self._connection and self._connection.is_open:
                self._connection.close()
                self.logger.info("RabbitMQ connection closed.")
                closed_something = True
        except Exception as e:
            self.logger.error("Error closing RabbitMQ connection: %s", e)

        if not closed_something:
            self.logger.info(
                "RabbitMQ connection/channel already closed or not established."
            )

        self._channel = None
        self._connection = None

    def check_single_message(self) -> Optional[Dict[str, Any]]:
        """
        Check for a single message without starting continuous consumption.
        Useful for polling-style message checking.
        
        Returns:
        - Dict or None: Message data if available, None otherwise
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
                    self.logger.info(
                        "Retrieved single message from queue `%s`: %s",
                        self.queue_name,
                        message_data,
                    )
                    return message_data
                except json.JSONDecodeError as e:
                    self.logger.error(
                        "Failed to decode JSON message: %s. Message body: %s", e, body
                    )
            return None

        except Exception as e:
            self.logger.error("Error checking for single message: %s", e)
            return None