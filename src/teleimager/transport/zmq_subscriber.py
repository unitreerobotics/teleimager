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


"""Receive the latest JPEG frames and optionally decode BGR in a background worker."""

from collections import deque
from typing import Dict, Optional, Tuple
import contextlib
import queue
import sys
import threading
import time

import logging_mp
import zmq

from ..frame import TeleImage, TripleRingBuffer

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


STALL_SECONDS = 3.0


try:
    from turbojpeg import TurboJPEG
    _turbojpeg = TurboJPEG()
except Exception as e:
    logger_mp.error(
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
    )
    sys.exit(1)


class SimpleFPSMonitor:
    """Estimate receive FPS from a full rolling window of frame intervals."""

    def __init__(self, window_size: int):
        """Initialize the interval window with an unknown receive rate."""
        self._times = deque(maxlen=window_size)
        self._last_tick = None
        self._fps = 0.0

    def tick(self):
        """Record a receive interval, ignoring ticks under 100 microseconds apart."""
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
        """Clear interval history and reset the measured receive rate to zero."""
        self._times.clear()
        self._last_tick = None
        self._fps = 0.0

    @property
    def fps(self) -> float:
        """Return 0.0 until the sampling window is fully populated."""
        return self._fps


class ZMQ_SubscriberThread(threading.Thread):
    """Own a SUB socket and latest-frame buffers, with optional background decoding."""

    def __init__(self, host: str, port: int, context: Optional[zmq.Context] = None, request_bgr: bool = False):
        """Prepare the subscriber and optional BGR decoder worker.

        Args:
            host: Server hostname or IP address to connect to.
            port: Server JPEG publication port.
            context: Shared ZMQ context; defaults to Context.instance().
            request_bgr: Start a background JPEG-to-BGR decoder.
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
        """Create a latest-frame decode queue, output buffer, and decoder thread."""
        self._bgr_3ring_buffer = TripleRingBuffer()
        self._bgr_decode_queue = queue.Queue(maxsize=1)
        self._decoder_thread = threading.Thread(target=self._decoder_loop, daemon=True)
        self._decoder_thread.start()

    def _decode_image(self, jpg_bytes):
        """Decode JPEG to BGR, or return None for missing or invalid data."""
        if jpg_bytes is None:
            return None
        try:
            return _turbojpeg.decode(jpg_bytes)
        except Exception as e:
            logger_mp.warning(f"[Teleimager] Failed to decode image: {e}")
            return None

    def ensure_bgr_enabled(self):
        """Enable background BGR decoding without replacing the subscriber."""
        if self._request_bgr:
            return
        self._init_bgr_decoder()
        self._request_bgr = True

    def _decoder_loop(self):
        """Consume queued JPEG bytes and update the independent BGR buffer."""
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
        """Wait for socket setup and return whether the subscriber became ready."""
        return self._started.wait(timeout=timeout)

    # Public API.
    def recv(self) -> TeleImage:
        """Read the current buffers without waiting for a new frame.

        Returns:
            A TeleImage with receive FPS and optional JPEG and BGR data. JPEG and
            BGR buffers update independently and may represent different frames.
        """
        current_fps = self._fps_monitor.fps
        jpg_data = self._jpg_3ring_buffer.read()
        if not self._request_bgr:
            return TeleImage(fps=current_fps, jpg=jpg_data)

        bgr_data = self._bgr_3ring_buffer.read()
        return TeleImage(fps=current_fps, jpg=jpg_data, bgr=bgr_data)

    def stop(self) -> None:
        """Request shutdown and wait briefly for the subscriber and decoder threads."""
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
        """Own the SUB socket and receive into latest-frame buffers.

        Keep transport and decode queues short to limit latency. Empty polls clear
        the JPEG buffer; a sustained stall resets the measured receive rate.
        """
        try:
            # Keep socket creation, I/O, and closure on the same worker.
            self._socket = self._context.socket(zmq.SUB)
            self._socket.setsockopt(zmq.CONFLATE, 1)  # Only keep latest message
            self._socket.setsockopt(zmq.RCVHWM, 1)    # Limit receive-side backlog
            self._socket.setsockopt(zmq.LINGER, 0)
            self._socket.connect(f"tcp://{self._host}:{self._port}")
            self._socket.setsockopt_string(zmq.SUBSCRIBE, "")

            poller = zmq.Poller()
            poller.register(self._socket, zmq.POLLIN)

            self._started.set()

            empty_polls = 0
            stall_threshold = max(1, int(STALL_SECONDS * 1000 / 100))
            while self._running:
                events = dict(poller.poll(timeout=100))
                if self._socket in events:
                    try:
                        img_bytes = self._socket.recv()
                        self._jpg_3ring_buffer.write(img_bytes)
                        # Decode independently so JPEG reception stays responsive.
                        if self._request_bgr:
                            try:
                                if self._bgr_decode_queue.full():
                                    self._bgr_decode_queue.get_nowait()
                                self._bgr_decode_queue.put_nowait(img_bytes)
                            except queue.Full:
                                pass
                        self._fps_monitor.tick()
                        empty_polls = 0

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

                    empty_polls += 1
                    if empty_polls >= stall_threshold:
                        self._fps_monitor.reset()
                    logger_mp.debug(f"[Teleimager] No message received from {self._host}:{self._port} within timeout.")
        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to initialize subscriber socket: {e}")
        finally:
            if self._socket:
                try:
                    self._socket.close()
                except Exception as e:
                    logger_mp.warning(f"[Teleimager] Error closing socket in cleanup: {e}")
                self._socket = None


class ZMQ_SubscriberManager:
    """Share workers by remote (host, port) and count manager acquisitions."""

    _instance: Optional["ZMQ_SubscriberManager"] = None
    _subscriber_threads: Dict[Tuple[str, int], ZMQ_SubscriberThread] = {}
    _lock = threading.Lock()
    _ref_count = 0
    _running = True

    def __init__(self):
        """Create the ZMQ context shared by subscriber workers."""
        self._context = zmq.Context()
        self._running = True

    def _create_subscriber_thread(self, host: str, port: int, request_bgr: bool = False) -> ZMQ_SubscriberThread:
        """Start a subscriber and raise ConnectionError if setup does not complete."""
        try:
            subscriber_thread = ZMQ_SubscriberThread(host, port, self._context, request_bgr)
            subscriber_thread.start()
            if not subscriber_thread._wait_for_start(timeout=1.0):
                raise ConnectionError(f"Subscriber thread failed to start for {host}:{port}")
            return subscriber_thread
        except Exception as e:
            logger_mp.error(f"[Teleimager] Failed to create subscriber thread for {host}:{port}: {e}")
            raise

    def _get_subscriber_thread(self, host: str, port: int, request_bgr: bool = False) -> ZMQ_SubscriberThread:
        """Reuse an endpoint worker and enable BGR decoding if requested."""
        key = (host, port)
        with self._lock:
            if key not in self._subscriber_threads:
                self._subscriber_threads[key] = self._create_subscriber_thread(host, port, request_bgr)
            elif request_bgr:
                self._subscriber_threads[key].ensure_bgr_enabled()
            return self._subscriber_threads[key]

    # Public API.
    @classmethod
    def get_instance(cls) -> "ZMQ_SubscriberManager":
        """Acquire a reference to the shared manager, creating it when needed."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            cls._ref_count += 1
            return cls._instance

    def subscribe(self, host: str, port: int, request_bgr: bool = False) -> TeleImage:
        """Read the latest image, starting an endpoint worker on first use.

        Args:
            host: Server hostname or IP address.
            port: Server JPEG publication port.
            request_bgr: Enable BGR decoding for this shared endpoint.

        Returns:
            A TeleImage containing the current receive rate and buffered data.

        Raises:
            RuntimeError: The manager has been closed.
            ConnectionError: A new subscriber does not become ready in time.
        """
        if not self._running:
            raise RuntimeError("SubscriberManager is closed.")

        subscriber_thread = self._get_subscriber_thread(host, port, request_bgr=request_bgr)
        return subscriber_thread.recv()

    def close(self) -> None:
        """Release a reference, stopping workers and the context on final release."""
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
