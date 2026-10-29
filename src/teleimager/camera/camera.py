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

"""Define common camera settings, output buffers, and readiness signaling."""

import threading

from turbojpeg import TurboJPEG
import logging_mp

from ..frame import TripleRingBuffer

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


_turbojpeg = TurboJPEG()


class BaseCamera:
    """Define the capture interface and latest-frame output buffers.

    Subclasses update enabled buffers in _update_frame() and own their capture
    resources. JPEG output serves ZMQ; BGR output serves WebRTC and App streams.
    Buffer reads return references without waiting for another capture.
    """

    # Hardware reads already block on capture. Extra pacing would prevent
    # draining a driver backlog after a stall; shared-memory reads need pacing.
    _capture_requires_pacing = False

    def __init__(self, cam_topic, server_cfg):
        """Read shared capture settings and allocate enabled output buffers.

        Args:
            cam_topic: Camera topic used to identify the source.
            server_cfg: Native camera mapping; image_shape is (height, width)
                and fps is the configured capture or publication rate.
        """
        self._ready = threading.Event()
        self._cam_topic = cam_topic
        self._img_shape = server_cfg.get("image_shape", None)
        self._fps = server_cfg.get("fps", 30)
        self._enable_zmq = server_cfg.get("enable_zmq", False)
        self._zmq_port = server_cfg.get("zmq_port", None)
        if self._enable_zmq:
            self._zmq_buffer = TripleRingBuffer()
        else:
            self._zmq_buffer = None

        self._enable_webrtc = server_cfg.get("enable_webrtc", False)
        self._webrtc_port = server_cfg.get("webrtc_port", None)
        self._webrtc_codec = server_cfg.get("webrtc_codec", None)
        self._enable_bgr_output = self._enable_webrtc
        if self._enable_bgr_output:
            self._bgr_buffer = TripleRingBuffer()
        else:
            self._bgr_buffer = None

    def __str__(self):
        raise NotImplementedError

    def __repr__(self):
        return self.__str__()

    def _update_frame(self):
        """Capture a frame and refresh enabled buffers; subclasses must implement it."""
        raise NotImplementedError

    def wait_until_ready(self, timeout=None):
        """Wait for backend readiness and return whether it was signaled.

        Hardware backends normally signal after a frame; IsaacSim signals on
        initialization and may still be waiting for shared-memory image data.
        """
        return self._ready.wait(timeout=timeout)

    def enable_webrtc(self):
        """Return whether WebRTC output is configured."""
        return self._enable_webrtc

    def enable_zmq(self):
        """Return whether ZMQ output is configured."""
        return self._enable_zmq

    def get_jpeg_bytes(self):
        """Return the latest JPEG bytes, or None if disabled or unavailable."""
        jpeg_bytes = self._zmq_buffer.read() if self._enable_zmq and self._zmq_buffer else None
        return jpeg_bytes

    def get_bgr_frame(self):
        """Return the latest BGR array, or None if disabled or unavailable."""
        bgr_numpy = self._bgr_buffer.read() if self._enable_bgr_output and self._bgr_buffer else None
        return bgr_numpy

    def enable_bgr_output(self):
        """Enable a BGR output buffer independently of WebRTC for App streaming."""
        if self._bgr_buffer is None:
            self._bgr_buffer = TripleRingBuffer()
        self._enable_bgr_output = True

    def get_depth_frame(self):
        """Return None by default; depth-capable backends override this method."""
        return None

    def get_zmq_port(self):
        """Return the configured ZMQ publication port."""
        return self._zmq_port

    def get_webrtc_port(self):
        """Return the configured WebRTC HTTPS port."""
        return self._webrtc_port

    def get_webrtc_codec(self):
        """Return the configured WebRTC codec preference."""
        return self._webrtc_codec

    def get_fps(self):
        """Return the configured frame rate in frames per second."""
        return self._fps

    def release(self):
        """Release capture resources; subclasses must implement this method."""
        raise NotImplementedError
