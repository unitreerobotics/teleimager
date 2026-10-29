# Copyright 2025-2026 YuShu TECHNOLOGY CO.,LTD ("Unitree Robotics")
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ------------------------------------------------------------------------------
# NOTICE: This file is modified by Unitree Robotics based on portions of
# the "beavr-bot" project (https://github.com/ARCLab-MIT/beavr-bot),
# which is licensed under the MIT License.
# ------------------------------------------------------------------------------


"""Publish JPEG bytes through per-endpoint ZMQ worker threads."""

from typing import Any, Dict, Optional, Tuple
import contextlib
import queue
import threading

import logging_mp
import zmq

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class ZMQ_PublisherThread(threading.Thread):
    """Own a PUB socket in a worker and send from a single-message queue."""

    def __init__(self, port: int, host: str = "0.0.0.0", context: Optional[zmq.Context] = None):
        """Prepare a publisher worker without opening its socket.

        Args:
            port: Local TCP port to bind.
            host: Local bind address; defaults to all IPv4 interfaces.
            context: ZMQ context supplied by the publisher manager.
        """
        super().__init__(daemon=True)
        self._port = port
        self._host = host
        self._context = context
        self._socket = None
        self._running = True
        self._queue = queue.Queue(maxsize=1)  # Bound latency by keeping one payload.
        self._started = threading.Event()

    def send(self, data: Any) -> None:
        """Queue the latest byte payload, dropping an older queued message.

        Args:
            data: Bytes, bytearray, or memoryview to publish without serialization.

        Raises:
            TypeError: The payload is not bytes-like.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"PublisherThread expects bytes, got {type(data)}")

        dropped_old = False
        if self._queue.full():
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
                dropped_old = True
        self._queue.put_nowait(data)

        if dropped_old:
            logger_mp.debug(f"[Teleimager] Publisher queue full for {self._host}:{self._port}, dropped old message")

    def stop(self) -> None:
        """Request shutdown, wake the queue, and wait briefly for the worker."""
        self._running = False

        if self._queue.full():
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()

        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

        self.join(timeout=1)
        if self.is_alive():
            logger_mp.warning("[Teleimager] Publisher thread did not stop gracefully")

    def run(self) -> None:
        """Create, use, and close the PUB socket in the worker thread."""
        try:
            # Keep socket creation, I/O, and closure on the same worker.
            self._socket = self._context.socket(zmq.PUB)
            self._socket.setsockopt(zmq.CONFLATE, 1)  # Only keep latest message
            self._socket.setsockopt(zmq.SNDHWM, 1)    # Limit send-side backlog
            self._socket.setsockopt(zmq.LINGER, 0)
            self._socket.bind(f"tcp://{self._host}:{self._port}")

            self._started.set()
            while self._running:
                try:
                    # Wake periodically to observe shutdown requests.
                    data = self._queue.get(timeout=0.1)

                    if data is None:
                        break

                    try:
                        self._socket.send(data, zmq.NOBLOCK)
                    except zmq.Again:
                        logger_mp.warning(f"[Teleimager] High water mark reached for at {self._host}:{self._port}, dropping message")
                    except zmq.ZMQError as e:
                        logger_mp.error(f"[Teleimager] Failed to publish to at {self._host}:{self._port}: {e}")
                        break

                except queue.Empty:
                    continue
                except Exception as e:
                    if self._running:
                        logger_mp.error(f"[Teleimager] Error in publisher loop: {e}")
                    break

        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to initialize publisher socket: {e}")
        finally:
            if self._socket:
                try:
                    self._socket.close()
                except Exception as e:
                    logger_mp.warning(f"[Teleimager] Error closing socket in cleanup: {e}")
                self._socket = None

    def wait_for_start(self, timeout: float = 1.0) -> bool:
        """Wait for the PUB socket to bind and return whether it became ready."""
        return self._started.wait(timeout=timeout)


class ZMQ_PublisherManager:
    """Reuse one ZMQ publisher worker per local (host, port) endpoint."""

    _instance: Optional["ZMQ_PublisherManager"] = None
    _publisher_threads: Dict[Tuple[str, int], ZMQ_PublisherThread] = {}
    _lock = threading.Lock()
    _running = True

    def __init__(self):
        """Create the ZMQ context shared by publisher workers."""
        self._context = zmq.Context()

    def _create_publisher_thread(self, port: int, host: str = "0.0.0.0") -> ZMQ_PublisherThread:
        """Start a publisher and raise ConnectionError if it fails to become ready."""
        try:
            publisher_thread = ZMQ_PublisherThread(port, host, self._context)
            publisher_thread.start()
            if not publisher_thread.wait_for_start(timeout=5.0):
                raise ConnectionError(f"Publisher thread failed to start for {host}:{port}")

            return publisher_thread
        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to create publisher thread for {host}:{port}: {e}")
            raise

    def _get_publisher_thread(self, port: int, host: str = "0.0.0.0") -> ZMQ_PublisherThread:
        """Return or create the endpoint worker while holding the manager lock."""
        key = (host, port)
        with self._lock:
            if key not in self._publisher_threads:
                self._publisher_threads[key] = self._create_publisher_thread(port, host)
            return self._publisher_threads[key]

    def _close_publisher(self, key: Tuple[str, int]) -> None:
        """Stop and remove one endpoint worker under the manager lock."""
        with self._lock:
            if key in self._publisher_threads:
                try:
                    self._publisher_threads[key].stop()
                except Exception as e:
                    logger_mp.error(f"[Teleimager] Error stopping publisher at {key[0]}:{key[1]}: {e}")
                del self._publisher_threads[key]

    # Public API.
    @classmethod
    def get_instance(cls) -> "ZMQ_PublisherManager":
        """Return or create the process-wide publisher manager under its lock."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def publish(self, data: Any, port: int, host: str = "0.0.0.0") -> None:
        """Queue bytes for an endpoint, creating its publisher on first use.

        Args:
            data: Bytes-like payload, normally an encoded JPEG image.
            port: Local publication port.
            host: Local bind address.

        Raises:
            RuntimeError: The manager has been closed.
            ConnectionError: A new publisher does not become ready in time.
            TypeError: The payload is not bytes-like.
        """
        if not self._running:
            raise RuntimeError("ZMQPublisherManager is closed")

        try:
            publisher_thread = self._get_publisher_thread(port, host)
            publisher_thread.send(data)
        except Exception as e:
            logger_mp.error(f"[Teleimager] Unexpected error in publish: {e}")
            raise

    def close(self) -> None:
        """Mark the manager closed and stop and remove all publisher workers."""
        self._running = False
        with self._lock:
            for key, publisher_thread in list(self._publisher_threads.items()):
                try:
                    publisher_thread.stop()
                except Exception as e:
                    logger_mp.error(f"[Teleimager] Error stopping publisher at {key[0]}:{key[1]}: {e}")
            self._publisher_threads.clear()
