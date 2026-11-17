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
import av
import logging_mp

from ..frame import DepthPacket, TripleRingBuffer

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


_turbojpeg = TurboJPEG()


def _jpeg_to_yuv_frame(jpeg_bytes):
    """Decode JPEG bytes into a YUV VideoFrame without a BGR round trip.

    The chroma plane shape selects the pixel format: 4:2:0, 4:2:2, or 4:4:4.
    """
    planes = _turbojpeg.decode_to_yuv_planes(jpeg_bytes)
    h, w = planes[0].shape
    if planes[1].shape == (h // 2, w // 2):
        pix_fmt = "yuvj420p"
    elif planes[1].shape == (h, w // 2):
        pix_fmt = "yuvj422p"
    else:
        pix_fmt = "yuvj444p"
    frame = av.VideoFrame(w, h, format=pix_fmt)
    for i, data in enumerate(planes):
        frame.planes[i].update(data)
    return frame


class BaseCamera:
    """Define the capture interface and latest-frame output buffers.

    Subclasses update enabled buffers in _update_frame() and own their capture
    resources. Each output has its own buffer: JPEG bytes for color ZMQ,
    (frame number, depth image, depth scale) tuples for depth ZMQ, and
    av.VideoFrame objects for WebRTC, written only while a WebRTC client is
    connected. Publishers wait on these buffers, so a frame goes out when it
    arrives and a stopped camera sends nothing. BGR arrays and display frames
    serve the App stream only: backends that decode a frame larger than
    image_shape fill the display buffer at the requested display shape; the
    others return their BGR output. BGR and display reads return references
    without waiting for another capture.
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

        Raises:
            ValueError: An enabled output has no port.
            RuntimeError: Depth is enabled but zstandard is not installed.
        """
        self._ready = threading.Event()
        self._cam_topic = cam_topic
        self._img_shape = server_cfg.get("image_shape", None)
        self._fps = server_cfg.get("fps", 30)

        # Color stream over ZMQ: JPEG bytes.
        self._enable_color_zmq = server_cfg.get("enable_color_zmq", False)
        self._color_zmq_port = server_cfg.get("color_zmq_port", None)
        if self._enable_color_zmq:
            if self._color_zmq_port is None:
                raise ValueError(f"[Teleimager] {cam_topic}: enable_color_zmq requires color_zmq_port")
            self._color_zmq_buffer = TripleRingBuffer()
        else:
            self._color_zmq_buffer = None
        self._color_zmq_seq = 0

        # Depth stream over ZMQ (RealSense only): enable_depth_zmq captures
        # aligned depth, depth_zmq_port publishes it on a separate port.
        self._enable_depth_zmq = server_cfg.get("enable_depth_zmq", False)
        self._depth_zmq_port = server_cfg.get("depth_zmq_port", None)
        if self._enable_depth_zmq:
            if self._depth_zmq_port is None:
                raise ValueError(f"[Teleimager] {cam_topic}: enable_depth_zmq requires depth_zmq_port")
            if not DepthPacket.available:
                raise RuntimeError("[Teleimager] depth streaming requires zstandard: pip install 'teleimager[depth]'")
            self._depth_zmq_buffer = TripleRingBuffer()
        else:
            self._depth_zmq_buffer = None
        self._depth_zmq_seq = 0

        # WebRTC: color only, written while a client is connected.
        self._enable_webrtc = server_cfg.get("enable_webrtc", False)
        self._webrtc_port = server_cfg.get("webrtc_port", None)
        self._webrtc_codec = server_cfg.get("webrtc_codec", None)
        if self._enable_webrtc:
            if self._webrtc_port is None:
                raise ValueError(f"[Teleimager] {cam_topic}: enable_webrtc requires webrtc_port")
            self._webrtc_buffer = TripleRingBuffer()
        else:
            self._webrtc_buffer = None
        self._webrtc_seq = 0
        self._webrtc_connected = threading.Event()  # Replaced by the publisher's event.

        # App stream: BGR and display output, enabled by the server per topic.
        self._enable_bgr_output = False
        self._bgr_buffer = None
        self._display_shape = None
        self._display_buffer = None

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

    def enable_color_zmq(self):
        """Return whether color ZMQ output is configured."""
        return self._enable_color_zmq

    def get_color_zmq_port(self):
        """Return the configured color ZMQ publication port."""
        return self._color_zmq_port

    def get_color_jpeg_bytes(self, timeout=None):
        """Wait for the next color JPEG frame.

        Args:
            timeout: Longest wait in seconds, or None to wait indefinitely.

        Returns:
            JPEG bytes newer than the previous call's, or None if color ZMQ is
            disabled or no new frame arrives within timeout.
        """
        if self._color_zmq_buffer is None:
            return None
        seq = self._color_zmq_buffer.wait_new(self._color_zmq_seq, timeout=timeout)
        if seq == self._color_zmq_seq:
            return None
        self._color_zmq_seq = seq
        return self._color_zmq_buffer.read()

    def enable_depth_zmq(self):
        """Return whether depth capture and depth ZMQ output are configured."""
        return self._enable_depth_zmq

    def get_depth_zmq_port(self):
        """Return the configured depth ZMQ publication port."""
        return self._depth_zmq_port

    def get_depth_zmq_message(self, timeout=None):
        """Wait for the next depth frame and encode it as a DepthPacket.

        Args:
            timeout: Longest wait in seconds, or None to wait indefinitely.

        Returns:
            Header and zstd payload bytes, or None if depth is disabled or no
            new frame arrives within timeout.
        """
        if self._depth_zmq_buffer is None:
            return None
        seq = self._depth_zmq_buffer.wait_new(self._depth_zmq_seq, timeout=timeout)
        if seq == self._depth_zmq_seq:
            return None
        self._depth_zmq_seq = seq
        item = self._depth_zmq_buffer.read()
        if item is None:
            return None
        return DepthPacket(*item).encode()

    def enable_webrtc(self):
        """Return whether WebRTC output is configured."""
        return self._enable_webrtc

    def get_webrtc_port(self):
        """Return the configured WebRTC HTTPS port."""
        return self._webrtc_port

    def get_webrtc_codec(self):
        """Return the configured WebRTC codec preference."""
        return self._webrtc_codec

    def set_webrtc_connected(self, event):
        """Share the publisher's client-connected event with the capture thread.

        Backends write WebRTC frames only while this event is set, so no frame
        is converted for WebRTC while nobody watches.
        """
        self._webrtc_connected = event

    def get_color_webrtc_frame(self, timeout=None):
        """Wait for the next WebRTC video frame.

        Args:
            timeout: Longest wait in seconds, or None to wait indefinitely.

        Returns:
            An av.VideoFrame, or None if WebRTC is disabled or no new frame
            arrives within timeout.
        """
        if self._webrtc_buffer is None:
            return None
        seq = self._webrtc_buffer.wait_new(self._webrtc_seq, timeout=timeout)
        if seq == self._webrtc_seq:
            return None
        self._webrtc_seq = seq
        return self._webrtc_buffer.read()

    def enable_bgr_output(self):
        """Enable the BGR output buffer that the App stream reads."""
        if self._bgr_buffer is None:
            self._bgr_buffer = TripleRingBuffer()
        self._enable_bgr_output = True

    def get_bgr_frame(self):
        """Return the latest BGR array, or None if disabled or unavailable."""
        bgr_numpy = self._bgr_buffer.read() if self._enable_bgr_output and self._bgr_buffer else None
        return bgr_numpy

    def set_display_shape(self, shape):
        """Select the (height, width) of display frames, or None to stop them."""
        if shape is None:
            self._display_shape = None
            return
        self._display_shape = (int(shape[0]), int(shape[1]))
        if self._display_buffer is None:
            self._display_buffer = TripleRingBuffer()

    def get_display_frame(self):
        """Return the latest display frame, or None when none is selected.

        Backends without a larger native frame return their BGR output; the
        App stream fits it into its rectangle.
        """
        if self._display_shape is None:
            return None
        return self.get_bgr_frame()

    def get_source_shape(self):
        """Return the (height, width) of one captured frame, or None if unknown."""
        if self._img_shape is None:
            return None
        return (int(self._img_shape[0]), int(self._img_shape[1]))

    def get_fps(self):
        """Return the configured frame rate in frames per second."""
        return self._fps

    def release(self):
        """Release capture resources; subclasses must implement this method."""
        raise NotImplementedError
