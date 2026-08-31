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

import time
import contextlib
import queue
import threading
from typing import Any, Dict, Optional, Tuple
import zmq
import numpy as np
import yaml
import os
from pathlib import Path
from collections import deque
import logging_mp
logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "teleimager"
CLIENT_CONFIG_PATH = str(CONFIG_DIR / "teleimager_client.yaml")

# Seconds of uninterrupted empty frames before the no-frame watchdog warns once.
STALL_SECONDS = 3.0

# Shared JPEG decoder (jpeg bytes -> BGR ndarray).
try:
    from turbojpeg import TurboJPEG
    _turbojpeg = TurboJPEG()
except Exception as e:
    raise RuntimeError(
        "\n"
        "  [Teleimager] Failed to initialize TurboJPEG.\n"
        "\n"
        "  Step 1: Install the Python binding:\n"
        "      pip install PyTurboJPEG\n"
        "  (Note: Do NOT install the PyPI package named 'turbojpeg' - it is a different, incompatible library.)\n"
        "\n"
        "  Step 2: Install the native C library (libjpeg-turbo 3.0+ is required).\n"
        "\n"
        "  Choose one of the following methods:\n"
        "\n"
        "  [Method 1 - Conda] (Recommended, cross-platform, handles paths automatically):\n"
        "      conda install -c conda-forge libjpeg-turbo\n"
        "\n"
        "  [Method 2 - Compile from source] (For Ubuntu/Debian to get the latest 3.x):\n"
        "      git clone https://github.com/libjpeg-turbo/libjpeg-turbo.git\n"
        "      cd libjpeg-turbo && mkdir build && cd build\n"
        "      cmake -DCMAKE_INSTALL_PREFIX=/opt/libjpeg-turbo ..\n"
        "      make -j$(nproc) && sudo make install\n"
        "      echo 'export LD_LIBRARY_PATH=/opt/libjpeg-turbo/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc\n"
        "      source ~/.bashrc\n"
        "\n"
        "  [Method 3 - Homebrew] (For macOS):\n"
        "      brew install jpeg-turbo\n"
        "\n"
        f"  Original error: {e}"
    ) from e

# ========================================================
# Utility tools
# ========================================================
class TripleRingBuffer:
    def __init__(self):
        self.buffer = [None, None, None]
        self.write_index = 0            # Index where the next write will occur
        self.latest_index = -1          # Index of the latest written data
        self.read_index = -1            # Index of the current read data
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            self.buffer[self.write_index] = data
            self.latest_index = self.write_index
            self.write_index = (self.write_index + 1) % 3
            if self.write_index == self.read_index:
                self.write_index = (self.write_index + 1) % 3

    def read(self):
        with self.lock:
            if self.latest_index == -1:
                return None  # No data has been written yet
            self.read_index = self.latest_index
        return self.buffer[self.read_index]

class SimpleFPSMonitor:
    def __init__(self, window_size: int):
        self._times = deque(maxlen=window_size)
        self._last_tick = None
        self._fps = 0.0

    def tick(self):
        now = time.perf_counter_ns()

        if self._last_tick is not None:
            interval_ns = now - self._last_tick
            if interval_ns < 100_000:
                return
            
            self._times.append(interval_ns)
            if len(self._times) == self._times.maxlen:
                rolling_sum = sum(self._times)
                if rolling_sum > 0:
                    self._fps = (len(self._times) * 1_000_000_000.0) / rolling_sum
            else:
                self._fps = 0.0

        self._last_tick = now
    
    def reset(self):
        self._times.clear()
        self._last_tick = None
        self._fps = 0.0

    @property
    def fps(self) -> float:
        """Return 0.0 until the sampling window is fully populated."""
        return self._fps
# ========================================================
# ZMQ publish
# ========================================================
class ZMQ_PublisherThread(threading.Thread):
    """Thread that owns a PUB socket and handles publishing via a queue."""

    def __init__(self, port: int, host: str = "0.0.0.0", context: Optional[zmq.Context] = None):
        """Initialize publisher thread.

        Args:
            port: The port number to bind to.
            host: The host address to bind to (default: all interfaces "*").
        """
        super().__init__(daemon=True)
        self._port = port
        self._host = host
        self._context = context
        self._socket = None
        self._running = True
        self._queue = queue.Queue(maxsize=1)  # Only keep the latest message to prevent memory bloat
        self._started = threading.Event()

    def send(self, data: Any) -> None:
        """Send latest data to the publisher queue, dropping stale data if needed.

        Args:
            data: The data to publish
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
        """Stop the publisher thread gracefully."""
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
        """Main publisher loop with socket creation in worker thread."""
        try:
            # Create socket in the worker thread
            self._socket = self._context.socket(zmq.PUB)
            self._socket.setsockopt(zmq.CONFLATE, 1)  # Only keep latest message
            self._socket.setsockopt(zmq.SNDHWM, 1)    # Limit send-side backlog
            self._socket.setsockopt(zmq.LINGER, 0)
            self._socket.bind(f"tcp://{self._host}:{self._port}")

            # Signal that socket is ready
            self._started.set()
            while self._running:
                try:
                    # Get data from queue with timeout to allow checking _running
                    data = self._queue.get(timeout=0.1)

                    # Check for sentinel value
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
                    # Queue was empty, just continue
                    continue
                except Exception as e:
                    if self._running:
                        logger_mp.error(f"[Teleimager] Error in publisher loop: {e}")
                    break

        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to initialize publisher socket: {e}")
        finally:
            # Ensure socket is closed when thread exits
            if self._socket:
                try:
                    self._socket.close()
                except Exception as e:
                    logger_mp.warning(f"[Teleimager] Error closing socket in cleanup: {e}")
                self._socket = None

    def wait_for_start(self, timeout: float = 1.0) -> bool:
        """Wait until socket context is ready"""
        return self._started.wait(timeout=timeout)

class ZMQ_PublisherManager:
    """Centralized management of ZMQ publishers"""

    _instance: Optional["ZMQ_PublisherManager"] = None
    _publisher_threads: Dict[Tuple[str, int], ZMQ_PublisherThread] = {}
    _lock = threading.Lock()
    _running = True

    def __init__(self):
        self._context = zmq.Context()

    def _create_publisher_thread(self, port: int, host: str = "0.0.0.0") -> ZMQ_PublisherThread:
        try:
            publisher_thread = ZMQ_PublisherThread(port, host, self._context)
            publisher_thread.start()
            # Wait for the thread to start and socket to be ready
            if not publisher_thread.wait_for_start(timeout=5.0):  # Increase timeout to 5 seconds
                raise ConnectionError(f"Publisher thread failed to start for {host}:{port}")

            return publisher_thread
        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to create publisher thread for {host}:{port}: {e}")
            raise

    def _get_publisher_thread(self, port: int, host: str = "0.0.0.0") -> ZMQ_PublisherThread:
        key = (host, port)
        with self._lock:
            if key not in self._publisher_threads:
                self._publisher_threads[key] = self._create_publisher_thread(port, host)
            return self._publisher_threads[key]

    def _close_publisher(self, key: Tuple[str, int]) -> None:
        with self._lock:
            if key in self._publisher_threads:
                try:
                    self._publisher_threads[key].stop()
                except Exception as e:
                    logger_mp.error(f"[Teleimager] Error stopping publisher at {key[0]}:{key[1]}: {e}")
                del self._publisher_threads[key]
    
    # --------------------------------------------------------
    # public api
    # --------------------------------------------------------
    @classmethod
    def get_instance(cls) -> "ZMQ_PublisherManager":
        """Get or create the singleton instance with thread safety.
        Returns:
            The singleton ZMQPublisherManager instance
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def publish(self, data: Any, port: int, host: str = "0.0.0.0") -> None:
        """Publish data to queue-based communication.

        Args:
            data: The data to publish
            port: The port number
            host: The host address

        Raises:
            ConnectionError: If publishing fails
            SerializationError: If data serialization fails
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
        """Close all publishers."""
        self._running = False
        # close all publishers
        with self._lock:
            for key, publisher_thread in list(self._publisher_threads.items()):
                try:
                    publisher_thread.stop()
                except Exception as e:
                    logger_mp.error(f"[Teleimager] Error stopping publisher at {key[0]}:{key[1]}: {e}")
            self._publisher_threads.clear()

# ========================================================
# ZMQ subscribe
# ========================================================
class TeleImage:
    _NOT_SET = object()
    __slots__ = ['jpg', '_bgr', 'fps']

    def __init__(self, fps: float, jpg: Optional[bytes], bgr: Any = _NOT_SET):
        self.fps = fps
        self.jpg = jpg
        self._bgr = bgr

    @property
    def bgr(self) -> Optional[np.ndarray]:
        """ Get decoded BGR image if decoding is enabled and data is available."""
        # state 1: decoding disabled
        if self._bgr is TeleImage._NOT_SET:
            logger_mp.warning(f"[Teleimager] Accessing .bgr but decoding was DISABLED.")
            return None
        # state 2: decoding enabled but no data
        if self._bgr is None:
            logger_mp.debug(f"[Teleimager] Accessing .bgr but no image data received.")
            return None
        # state 3: decoding enabled and data available
        return self._bgr

    def __bool__(self):
        """ Truth value based on whether jpg byte data is available """
        return bool(self.jpg)

    def __iter__(self):
        """ Allow unpacking like: jpg, bgr, fps = teleimage_instance """
        yield self.fps
        yield self.jpg
        yield (None if self._bgr is TeleImage._NOT_SET else self._bgr)

    def __repr__(self):
        """ String representation for debugging """
        size = len(self.jpg) if self.jpg else 0
        state = "DISABLED" if self._bgr is TeleImage._NOT_SET else ("FAILED" if self._bgr is None else "OK")
        return f"TeleImage(fps={self.fps:.1f}, jpg_byte_size={size}, bgr_state={state})"
        

class ZMQ_SubscriberThread(threading.Thread):
    """Thread that owns a SUB socket and handles receiving the latest message."""

    def __init__(self, host: str, port: int, context: Optional[zmq.Context] = None, request_bgr: bool = False):
        """Initialize subscriber thread.

        Args:
            port: The port number to connect to.
            host: The server host address to connect to.
            context: Optional ZMQ context to use. If None, a new context will be created.
        """
        super().__init__(daemon=True)
        self._host = host
        self._port = port
        self._context = context or zmq.Context.instance()
        self._request_bgr = request_bgr

        self._socket = None
        self._running = True
        self._started = threading.Event()

        self._jpg_3ring_buffer = TripleRingBuffer()
        self._fps_monitor = SimpleFPSMonitor(window_size=10)
        if self._request_bgr:
            self._init_bgr_decoder()
        else:
            self._bgr_3ring_buffer = None
            self._bgr_decode_queue = None
            self._decoder_thread = None

    def _init_bgr_decoder(self):
        self._bgr_3ring_buffer = TripleRingBuffer()
        self._bgr_decode_queue = queue.Queue(maxsize=1)
        self._decoder_thread = threading.Thread(target=self._decoder_loop, daemon=True)
        self._decoder_thread.start()

    def _decode_image(self, jpg_bytes):
        """Decode JPEG bytes to a BGR ndarray via libturbojpeg."""
        if jpg_bytes is None:
            return None
        try:
            return _turbojpeg.decode(jpg_bytes)
        except Exception as e:
            logger_mp.warning(f"[Teleimager] Failed to decode image: {e}")
            return None

    def ensure_bgr_enabled(self):
        """Enable BGR decoding capability on demand without creating a new subscriber."""
        if self._request_bgr:
            return
        self._init_bgr_decoder()
        self._request_bgr = True

    def _decoder_loop(self):
        while self._running:
            try:
                jpg_bytes = self._bgr_decode_queue.get(timeout=0.1)
                if jpg_bytes is None:
                    self._bgr_decode_queue.task_done()
                    continue
                img_numpy = self._decode_image(jpg_bytes)
                self._bgr_3ring_buffer.write(img_numpy)
                self._bgr_decode_queue.task_done()
            except queue.Empty:
                continue
        
    def _wait_for_start(self, timeout: float = 1.0) -> bool:
        """Wait until socket context is ready"""
        return self._started.wait(timeout=timeout)

    # --------------------------------------------------------
    # public api
    # --------------------------------------------------------
    def recv(self) -> TeleImage:
        """Get the latest received message.

        Returns:
            The latest message as a TeleImage object containing raw bytes, decoded BGR image (if enabled), and FPS.
        """
        current_fps = self._fps_monitor.fps
        jpg_data = self._jpg_3ring_buffer.read()
        if not self._request_bgr:
            return TeleImage(fps=current_fps, jpg=jpg_data)

        bgr_data = self._bgr_3ring_buffer.read()
        return TeleImage(fps=current_fps, jpg=jpg_data, bgr=bgr_data)

    def stop(self) -> None:
        """Stop the subscriber thread gracefully."""
        self._running = False
        self.join(timeout=1.0)
        if self.is_alive():
            logger_mp.warning("[Teleimager] Subscriber thread did not stop gracefully")
        if self._decoder_thread is not None:
            with contextlib.suppress(queue.Full):
                self._bgr_decode_queue.put_nowait(None)
            self._decoder_thread.join(timeout=1.0)
            if self._decoder_thread.is_alive():
                logger_mp.warning("[Teleimager] Subscriber decoder thread did not stop gracefully")

    def run(self) -> None:
        """Main subscriber loop with socket creation in worker thread."""
        try:
            # Create socket in the worker thread
            self._socket = self._context.socket(zmq.SUB)
            self._socket.setsockopt(zmq.CONFLATE, 1)  # Only keep latest message
            self._socket.setsockopt(zmq.RCVHWM, 1)    # Limit receive-side backlog
            self._socket.setsockopt(zmq.LINGER, 0)
            self._socket.connect(f"tcp://{self._host}:{self._port}")
            self._socket.setsockopt_string(zmq.SUBSCRIBE, "")

            poller = zmq.Poller()
            poller.register(self._socket, zmq.POLLIN)

            # Signal that socket is ready
            self._started.set()
            while self._running:
                events = dict(poller.poll(timeout=100))
                if self._socket in events:
                    try:
                        # receive the latest message
                        img_bytes = self._socket.recv()
                        # write to 3-ring-buffer
                        self._jpg_3ring_buffer.write(img_bytes)
                        # enqueue for decoding if needed
                        if self._request_bgr:
                            try:
                                if self._bgr_decode_queue.full():
                                    self._bgr_decode_queue.get_nowait()
                                self._bgr_decode_queue.put_nowait(img_bytes)
                            except queue.Full:
                                pass
                        # update fps
                        self._fps_monitor.tick()
                        
                    except Exception as e:
                        if self._running:
                            logger_mp.error(f"[Teleimager] Error in subscriber loop: {e}")
                        break
                else:
                    self._jpg_3ring_buffer.write(None)
                    if self._request_bgr:
                        try:
                            if self._bgr_decode_queue.full():
                                self._bgr_decode_queue.get_nowait()
                            self._bgr_decode_queue.put_nowait(None)
                        except queue.Full:
                            pass

                    self._fps_monitor.reset()
                    logger_mp.debug(f"[Teleimager] No message received from {self._host}:{self._port} within timeout.")
        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to initialize subscriber socket: {e}")
        finally:
            # Ensure socket is closed when thread exits
            if self._socket:
                try:
                    self._socket.close()
                except Exception as e:
                    logger_mp.warning(f"[Teleimager] Error closing socket in cleanup: {e}")
                self._socket = None

class ZMQ_SubscriberManager:
    """Centralized management of ZMQ subscribers."""

    _instance: Optional["ZMQ_SubscriberManager"] = None
    _subscriber_threads: Dict[Tuple[str, int], ZMQ_SubscriberThread] = {}
    _lock = threading.Lock()
    _ref_count = 0
    _running = True

    def __init__(self):
        self._context = zmq.Context()
        self._running = True

    def _create_subscriber_thread(self, host: str, port: int, request_bgr: bool = False) -> ZMQ_SubscriberThread:
        try:
            subscriber_thread = ZMQ_SubscriberThread(host, port, self._context, request_bgr)
            subscriber_thread.start()
            # Wait for the thread to start and socket to be ready
            if not subscriber_thread._wait_for_start(timeout=1.0):
                raise ConnectionError(f"Subscriber thread failed to start for {host}:{port}")
            return subscriber_thread
        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to create subscriber thread for {host}:{port}: {e}")
            raise 

    def _get_subscriber_thread(self, host: str, port: int, request_bgr: bool = False) -> ZMQ_SubscriberThread:
        key = (host, port)
        with self._lock:
            if key not in self._subscriber_threads:
                self._subscriber_threads[key] = self._create_subscriber_thread(host, port, request_bgr)
            elif request_bgr:
                self._subscriber_threads[key].ensure_bgr_enabled()
            return self._subscriber_threads[key]
        
    # --------------------------------------------------------
    # public api
    # --------------------------------------------------------
    @classmethod
    def get_instance(cls) -> "ZMQ_SubscriberManager":
        """Get or create the singleton instance with thread safety."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            cls._ref_count += 1
            return cls._instance

    def subscribe(self, host: str, port: int, request_bgr: bool = False) -> TeleImage:
        """Receive the latest message from the specified subscriber.
        Args:
            host: The server address
            port: The port number
            request_bgr: Whether to request BGR decoding

        Returns:
            The latest message as a TeleImage object containing current fps, raw bytes and decoded BGR image (if enabled).
        """
        if not self._running:
            raise RuntimeError("SubscriberManager is closed.")

        subscriber_thread = self._get_subscriber_thread(host, port, request_bgr=request_bgr)
        return subscriber_thread.recv()

    def close(self) -> None:
        """Close all subscribers."""
        with self._lock:
            if self._ref_count > 1:
                self._ref_count -= 1
                return

            self._ref_count = 0
            self._running = False
            for key, subscriber in self._subscriber_threads.items():
                try:
                    subscriber.stop()
                except Exception as e:
                    logger_mp.error(f"[Teleimager] Error stopping subscriber at {key[0]}:{key[1]}: {e}")
            self._subscriber_threads.clear()
            type(self)._instance = None
        try:
            self._context.term()
        except Exception as e:
            logger_mp.warning(f"[Teleimager] Error closing SubscriberManager context: {e}")

# ========================================================
# ZMQ response
# ========================================================
class ZMQ_Responser:
    """ ZMQ REP socket to respond with camera configuration upon request."""
    def __init__(self, server_config, host: str = "0.0.0.0", port: int = 60000):
        """
        Args:
            server_config: The full server config to send in response to requests.
            host: Host/IP to bind.
            port: TCP port to bind.
            poll_timeout: Timeout in milliseconds for poll() to check for requests.
        """
        self._server_config = server_config
        self._host = host
        self._port = port
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.REP)
        self._socket.bind(f"tcp://{self._host}:{self._port}")
        self._running = True

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger_mp.info(f"[Teleimager] Camera Config Responser initialized at {self._host}:{self._port}")

    def _run(self):
        poller = zmq.Poller()
        poller.register(self._socket, zmq.POLLIN)
        while self._running:
            try:
                socks = dict(poller.poll(timeout=200))
                if self._socket in socks and socks[self._socket] == zmq.POLLIN:
                    _ = self._socket.recv()  # receive request
                    self._socket.send_json(self._server_config)
            except zmq.ZMQError as e:
                if not self._running:
                    break  # normal exit when stopping
                logger_mp.error(f"[Teleimager] ZMQError in Responser: {e}")
            except Exception as e:
                logger_mp.error(f"[Teleimager] Unexpected error in Responser: {e}")
    # --------------------------------------------------------
    # public api
    # --------------------------------------------------------
    def get_port(self):
        return self._port

    def stop(self):
        """Stop the Responser thread and close ZMQ resources."""
        self._running = False
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            logger_mp.warning("[Teleimager] Responser thread did not stop gracefully")
        try:
            self._socket.close()
            self._context.term()
        except Exception as e:
            logger_mp.warning(f"[Teleimager] Error closing Responser socket: {e}")

# ========================================================
# ZMQ request
# ========================================================
class ZMQ_Requester:
    """ ZMQ REQ socket to request camera configuration from server. If server is unreachable,
        fall back to the locally cached teleimager_client.yaml from the last successful fetch."""
    def __init__(self, host: str, port: int):
        """
        Args:
            host: IP or hostname of the server.
            port: TCP port of the server.
        """
        self._host = host
        self._port = port
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.REQ)
        self._socket.setsockopt(zmq.LINGER, 0)  # do not wait on close
        self._socket.connect(f"tcp://{self._host}:{self._port}")

        self._poller = zmq.Poller()
        self._poller.register(self._socket, zmq.POLLIN)

    @staticmethod
    def _load_local_config():
        """Load the camera table from the local cache of the last scanned server config."""
        camera_config = None
        if Path(CLIENT_CONFIG_PATH).exists():
            try:
                with open(CLIENT_CONFIG_PATH, "r") as f:
                    camera_config = yaml.safe_load(f)
                logger_mp.debug(f"[Teleimager] Loaded camera config from local {CLIENT_CONFIG_PATH}")
            except Exception as e:
                logger_mp.warning(f"[Teleimager] Failed to load local teleimager_client.yaml: {e}")
        else:
            logger_mp.error("[Teleimager] No local camera config cache found. Run 'teleimager-client' once to fetch it from the server.")

        if camera_config is None:
            raise RuntimeError("Failed to get camera configuration.")

        return camera_config["camera"]

    # --------------------------------------------------------
    # public api
    # --------------------------------------------------------
    def request(self) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Fetch the camera config. Returns (config, from_server): from_server is
        True only when the config came fresh off the network; on timeout/error it
        falls back to the local cache with from_server=False."""
        try:
            self._socket.send(b"GET_DATA")
            socks = dict(self._poller.poll(timeout=1000))

            if self._socket in socks and socks[self._socket] == zmq.POLLIN:
                camera_config = self._socket.recv_json()
                if camera_config is not None:
                    logger_mp.info(f"[Teleimager] Received camera config from server {self._host}:{self._port}")
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                    with open(CLIENT_CONFIG_PATH, "w") as f:
                        yaml.safe_dump(camera_config, f, sort_keys=False, allow_unicode=True)
                    logger_mp.info(f"[Teleimager] Saved camera config to local {CLIENT_CONFIG_PATH}")
                    return camera_config["camera"], True
            else:
                logger_mp.warning(f"[Teleimager] Request to {self._host}:{self._port} timed out or no response.")
        except Exception as e:
            logger_mp.error(f"[Teleimager] Unexpected error in Requester: {e}")

        # Network failed: best-effort fall back to the last cached config.
        try:
            return self._load_local_config(), False
        except Exception:
            return None, False

    def close(self):
        """Close the requester socket and terminate context."""
        try:
            self._socket.close()
            self._context.term()
        except Exception as e:
            logger_mp.warning(f"[Teleimager] Error closing Requester socket: {e}")

# ========================================================
# teleimage client
# ========================================================
class TeleImageClient:
    """Subscribe to a single camera stream identified by its topic.

    Per-topic singleton: constructing a client for a topic that already has a
    live client returns that existing instance (and logs a warning) instead of
    creating a second one.
    """
    _instances: Dict[str, "TeleImageClient"] = {}
    _instances_lock = threading.Lock()

    def __new__(cls, topic, *args, **kwargs):
        with cls._instances_lock:
            existing = cls._instances.get(topic)
            if existing is not None:
                logger_mp.warning(f"[Teleimager] Camera '{topic}' already has a client; returning the existing instance.")
                return existing
            instance = super().__new__(cls)
            cls._instances[topic] = instance
            return instance

    def __init__(self, camera_topic, server_host, zmq_port, request_bgr: bool = False):
        """
        Args:
            camera_topic:     Camera name, used only as a label (window title / logs / singleton key)
            server_host:      IP address of teleimager host server
            zmq_port:         Server port publishing this camera's stream (see the roster printed by scan())
            request_bgr:      Whether to request BGR decoding for subscribers
        """
        if getattr(self, "_initialized", False):
            return  # singleton reuse: already set up, do not re-subscribe
        self._initialized = True
        self._camera_topic = camera_topic
        self._server_host = server_host
        self._zmq_port = zmq_port
        self._request_bgr = request_bgr
        self._closed = False
        self._subscriber_manager = ZMQ_SubscriberManager.get_instance()
        self._subscriber_manager.subscribe(self._server_host, self._zmq_port, request_bgr=self._request_bgr)
        # No-frame watchdog state (edge-triggered warning; see get_frame).
        self._last_frame_ts = time.perf_counter()
        self._stalled = False
        logger_mp.info(f" 📷 Camera {self._camera_topic!r:<22} init ok (host={self._server_host}, zmq_port={self._zmq_port}, request_bgr={self._request_bgr})")

    # --------------------------------------------------------
    # public api
    # --------------------------------------------------------
    @classmethod
    def scan(cls, server_host, request_port=60000):
        requester = ZMQ_Requester(server_host, request_port)
        try:
            camera_config, from_server = requester.request()
        finally:
            requester.close()
        if camera_config is None:
            raise RuntimeError("Failed to get camera configuration.")
        else:
            if from_server:
                logger_mp.info(f"[Teleimager] Cameras currently available on teleimager-server @ {server_host}:")
            else:
                logger_mp.warning(f"[Teleimager] teleimager-server @ {server_host} unreachable — showing last cached cameras (may be stale):")

            # Print the roster as an aligned table so columns line up and scan vertically.
            def _shape(s):
                return f"{s[0]}x{s[1]}" if isinstance(s, (list, tuple)) and len(s) == 2 else str(s)
            topic_w = max((len(t) for t in camera_config), default=0)
            type_w = max((len(str(cfg.get('type'))) for cfg in camera_config.values()), default=0)
            shape_w = max((len(_shape(cfg.get('image_shape'))) for cfg in camera_config.values()), default=0)
            for topic, cfg in camera_config.items():
                zmq_on = 'on' if cfg.get('enable_zmq') else 'off'
                webrtc_on = 'on' if cfg.get('enable_webrtc') else 'off'
                logger_mp.info(
                    f" 📷 {topic:<{topic_w}}  {str(cfg.get('type')):<{type_w}}  {_shape(cfg.get('image_shape')):>{shape_w}}  "
                    f"zmq={zmq_on:<3}(port={cfg.get('zmq_port')})  webrtc={webrtc_on:<3}(port={cfg.get('webrtc_port')})"
                )
            # Show how to stream every zmq-enabled camera, instantiating each explicitly.
            topics = [t for t, cfg in camera_config.items() if cfg.get('enable_zmq')]
            if topics:
                lines = ["import cv2, time", "from teleimager.client import TeleImageClient", ""]
                for t in topics:
                    lines.append(f'{t} = TeleImageClient("{t}", server_host="{server_host}", zmq_port={camera_config[t]["zmq_port"]}, request_bgr=True)')
                lines.append("try:")
                lines.append("    while True:")
                for t in topics:
                    lines.append(f"        frame = {t}.get_frame()")
                    lines.append("        if frame.bgr is not None:")
                    lines.append(f'            cv2.imshow("{t}", frame.bgr)')
                lines.append("        if cv2.waitKey(1) & 0xFF == ord('q'):")
                lines.append("            break")
                lines.append("        time.sleep(0.002)")
                lines.append("finally:")
                for t in topics:
                    lines.append(f"    {t}.close()")
                lines.append("    cv2.destroyAllWindows()")
                logger_mp.info("[Teleimager] Copy-paste example to stream these cameras:\n```python\n" + "\n".join(lines) + "\n```")
        return camera_config, from_server

    def get_frame(self):
        frame = self._subscriber_manager.subscribe(self._server_host, self._zmq_port, request_bgr=self._request_bgr)
        # No-frame watchdog: edge-triggered, so a steady outage logs exactly once
        # (and recovery once). Pure comparisons — no blocking, no network, no scan.
        now = time.perf_counter()
        if frame.jpg is not None:
            self._last_frame_ts = now
            if self._stalled:
                self._stalled = False
                logger_mp.info(f" 📷 Camera {self._camera_topic!r:<22} frames resumed.")
        elif not self._stalled and now - self._last_frame_ts > STALL_SECONDS:
            self._stalled = True
            logger_mp.warning(f" 📷 Camera {self._camera_topic!r:<22} no frames for {STALL_SECONDS:g}s "
                              f"@ {self._server_host}:{self._zmq_port} — is the server running and this port correct?")
        return frame

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._subscriber_manager.close()
        with type(self)._instances_lock:
            type(self)._instances.pop(self._camera_topic, None)
        logger_mp.info(f" 📷 Camera {self._camera_topic!r:<22} closed.")


# ========================================================
# Teleimage Client
# ========================================================

def main():
    # command line args
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='192.168.123.164', help='IP address of image server')
    args = parser.parse_args()

    try:
        import cv2
    except ImportError:
        logger_mp.error(
            "[Teleimager] The teleimager-client viewer needs opencv. Install it with:\n"
            "    pip install \"teleimager[viewer]\""
        )
        return

    # Request the server config once (network-first, cached to local on success).
    camera_config, from_server = TeleImageClient.scan(server_host=args.host)
    if not from_server:
        logger_mp.error(f"[Teleimager] teleimager-server @ {args.host}:{60000} is unreachable. Check the server and retry.")
        return
    logger_mp.debug(f"[Teleimager] Camera config loaded: {camera_config}")

    topics = [t for t, cfg in camera_config.items() if cfg.get('enable_zmq')]
    if not topics:
        logger_mp.warning("[Teleimager] No ZMQ-enabled cameras to stream.")
        return

    # Visualize every ZMQ-enabled camera; press 'q' to quit.
    clients = {topic: TeleImageClient(topic, server_host=args.host, zmq_port=camera_config[topic]['zmq_port'], request_bgr=True)
               for topic in topics}

    running = True
    try:
        while running:
            for topic, client in clients.items():
                frame = client.get_frame()
                if frame.bgr is not None:
                    cv2.putText(frame.bgr, f"fps: {frame.fps:.1f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    cv2.imshow(topic, frame.bgr)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                running = False
            # Small delay to prevent excessive CPU usage
            time.sleep(0.002)
    finally:
        for client in clients.values():
            client.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
