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


"""Discover server camera settings and receive JPEG streams over ZMQ."""

from typing import Dict
import threading
import time

import logging_mp

from .transport.zmq_request import ZMQ_Requester
from .transport.zmq_subscriber import STALL_SECONDS, ZMQ_SubscriberManager

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class TeleImageClient:
    """Subscribe to a single camera stream identified by its topic.

    Per-topic singleton: constructing a client for a topic that already has a
    live client returns that existing instance (and logs a warning) instead of
    creating a second one.
    """

    _instances: Dict[str, "TeleImageClient"] = {}
    _instances_lock = threading.Lock()

    def __new__(cls, topic, *args, **kwargs):
        """Return the existing topic client or register a new instance."""
        with cls._instances_lock:
            existing = cls._instances.get(topic)
            if existing is not None:
                logger_mp.warning(f"[Teleimager] Camera '{topic}' already has a client; returning the existing instance.")
                return existing
            instance = super().__new__(cls)
            cls._instances[topic] = instance
            return instance

    def __init__(self, camera_topic, server_host, zmq_port, request_bgr: bool = False):
        """Subscribe once to the selected camera endpoint.

        Args:
            camera_topic: Camera label and per-topic singleton key.
            server_host: Hostname or IP address of the TeleImager server.
            zmq_port: Port publishing this camera's JPEG stream.
            request_bgr: Enable background JPEG-to-BGR decoding.
        """
        if getattr(self, "_initialized", False):
            return  # Reusing a topic must not acquire another subscription.
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

    # Public API.
    @classmethod
    def scan(cls, server_host, request_port=60000):
        """Fetch camera settings and log the roster and a subscription example.

        Returns:
            (camera_config, from_server), where camera_config maps topics to
            settings and from_server is False when the local cache was used.

        Raises:
            RuntimeError: Neither the server nor the cache provides settings.
        """
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
            # Generate one explicit client per ZMQ-enabled topic.
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
        """Read the latest buffered image without waiting for a new frame.

        Returns:
            A TeleImage with receive FPS, optional JPEG bytes, and optional BGR
            data. Repeated reads may return the same buffered values.
        """
        frame = self._subscriber_manager.subscribe(self._server_host, self._zmq_port, request_bgr=self._request_bgr)
        # Log only stall and recovery transitions to avoid repeated warnings.
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
        """Release this client's manager reference and remove its topic entry."""
        if self._closed:
            return
        self._closed = True
        self._subscriber_manager.close()
        with type(self)._instances_lock:
            type(self)._instances.pop(self._camera_topic, None)
        logger_mp.info(f" 📷 Camera {self._camera_topic!r:<22} closed.")


def main():
    """Discover live ZMQ cameras and display their decoded frames until quit."""
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
            # Limit viewer polling when no new frames are available.
            time.sleep(0.002)
    finally:
        for client in clients.values():
            client.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
