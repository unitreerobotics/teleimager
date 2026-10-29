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

"""Receive RTP/H264 or RTP/JPEG video and publish resized camera buffers."""

import os
import selectors
import socket
import subprocess
import threading
import time

from aiortc.codecs.h264 import H264Decoder, h264_depayload
from aiortc.jitterbuffer import JitterBuffer
from aiortc.rtp import RtpPacket
from av.video.reformatter import VideoReformatter
import av
import logging_mp
import numpy as np

from .camera import BaseCamera

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class RtpCamera(BaseCamera):
    """Receive RTP/H264 or RTP/JPEG video without pinning the source port."""

    def __init__(self, cam_topic, server_cfg):
        """Bind a UDP receiver and initialize the selected RTP decoder.

        Args:
            cam_topic: Camera topic used to identify the source.
            server_cfg: Camera mapping with rtp_port, image_shape, and optional
                bind_host, source_host, receive_timeout, and rtp_encoding.
        """
        import cv2

        super().__init__(cam_topic, server_cfg)
        self._cv2 = cv2
        bind_host = server_cfg.get("bind_host", "0.0.0.0")
        self._source_host = server_cfg.get("source_host", "192.168.123.161")
        rtp_port = server_cfg["rtp_port"]
        self._receive_timeout = server_cfg.get("receive_timeout", 2.0)
        self._rtp_encoding = server_cfg.get("rtp_encoding", "H264")
        height, width = self._img_shape
        if height <= 0 or width <= 0:
            raise ValueError("RTP image_shape requires a positive height and width.")
        if self._receive_timeout <= 0:
            raise ValueError("RTP receive_timeout must be positive.")
        if self._rtp_encoding not in ("H264", "JPEG"):
            raise ValueError("RTP rtp_encoding must be H264 or JPEG.")
        self._selector = selectors.DefaultSelector()
        self._socket = None
        self._jpeg_input = None
        self._jpeg_process = None
        self._closed = threading.Event()
        self._last_frame_time = None
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self._socket.bind((bind_host, rtp_port))
            self._socket.setblocking(False)
            self._selector.register(self._socket, selectors.EVENT_READ)
            if self._rtp_encoding == "JPEG":
                # Filter sources in Python, retaining RTP datagram boundaries for
                # fdsrc. A slow decoder drops packets rather than blocking capture.
                self._jpeg_input, pipeline_input = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                with pipeline_input:
                    self._jpeg_input.setblocking(False)
                    self._jpeg_input.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                    # Both static PT 26 and dynamic PT 96 are used by firmware.
                    self._jpeg_process = subprocess.Popen(
                        ["gst-launch-1.0", "-q", "fdsrc", f"fd={pipeline_input.fileno()}",
                         "blocksize=65536", "do-timestamp=true", "!",
                         "application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG",
                         "!", "rtpjitterbuffer", "latency=20", "drop-on-latency=true",
                         "!", "rtpjpegdepay", "!", "jpegparse",
                         "!", "fdsink", "fd=1", "sync=false"],
                        pass_fds=(pipeline_input.fileno(),), stdout=subprocess.PIPE,
                    )
                os.set_blocking(self._jpeg_process.stdout.fileno(), False)
                self._decoder = av.CodecContext.create("mjpeg", "r")
                self._decoder.thread_count = 1
                self._selector.register(self._jpeg_process.stdout, selectors.EVENT_READ)
            else:
                self._decoder = H264Decoder()
                self._decoder.codec.thread_count = 1
                self._jitter = JitterBuffer(capacity=128, is_video=True)
                self._reformatter = VideoReformatter()
                self._ssrc = None
                self._decode_frame_time = None
        except Exception:
            self.release()
            raise
        logger_mp.info(str(self))

    def __str__(self):
        return (f"[RtpCamera: {self._cam_topic}] RTP/{self._rtp_encoding} port={self._socket.getsockname()[1]}, "
                f"output={self._img_shape[1]}x{self._img_shape[0]} @ {self._fps} FPS")

    def _update_frame(self):
        """Wait for a decoded frame, then resize it with aspect ratio preserved.

        Filters datagrams by source IP. H264 uses a jitter buffer; JPEG uses a
        GStreamer depayloader. Missing frames clear readiness and raise TimeoutError
        after receive_timeout seconds; decoded output is padded with black borders.
        """
        started_at = time.monotonic()
        while not self._closed.is_set():
            remaining = self._receive_timeout - (time.monotonic() - started_at)
            if remaining <= 0:
                self._ready.clear()
                raise TimeoutError(f"RTP {self._cam_topic} received no fresh frame for {self._receive_timeout}s")
            bgr = None
            try:
                for key, _ in self._selector.select(min(remaining, 0.05)):
                    if key.fileobj is not self._socket:
                        data = os.read(key.fileobj.fileno(), 256 * 1024)
                        if not data:
                            if self._closed.is_set():
                                return
                            raise OSError("RTP/JPEG receiver exited; inspect GStreamer stderr.")
                        for packet in self._decoder.parse(data):
                            decoded = self._cv2.imdecode(np.frombuffer(packet, dtype=np.uint8), self._cv2.IMREAD_COLOR)
                            if decoded is not None:
                                bgr = decoded
                        continue
                    # Bound each drain so other streams and JPEG output can run.
                    for _ in range(128):
                        try:
                            data, address = self._socket.recvfrom(65535)
                        except BlockingIOError:
                            break
                        if address[0] != self._source_host:
                            continue
                        if self._rtp_encoding == "JPEG":
                            try:
                                self._jpeg_input.send(data)
                            except BlockingIOError:
                                pass
                            continue
                        received_at = time.monotonic()
                        try:
                            packet = RtpPacket.parse(data)
                            if packet.payload_type != 96:
                                continue
                            packet._data = h264_depayload(packet.payload)
                        except ValueError:
                            continue
                        stalled = self._decode_frame_time is not None and received_at - self._decode_frame_time > max(0.25, 3.0 / self._fps)
                        if self._ssrc != packet.ssrc or stalled:
                            self._ssrc = packet.ssrc
                            self._jitter = JitterBuffer(capacity=128, is_video=True)
                            self._decoder = H264Decoder()
                            self._decoder.codec.thread_count = 1
                            bgr = None
                            self._decode_frame_time = received_at
                        lost_frame, encoded = self._jitter.add(packet)
                        if lost_frame:
                            self._decoder = H264Decoder()
                            self._decoder.codec.thread_count = 1
                            bgr = None
                        if encoded is not None:
                            self._decode_frame_time = received_at
                            for frame in self._decoder.decode(encoded):
                                bgr = self._reformatter.reformat(frame, format="bgr24").to_ndarray()
            except (OSError, ValueError):
                if self._closed.is_set():
                    return
                raise
            if bgr is None:
                continue
            height, width = self._img_shape
            scale = min(width / bgr.shape[1], height / bgr.shape[0])
            resized_width = max(1, round(bgr.shape[1] * scale))
            resized_height = max(1, round(bgr.shape[0] * scale))
            output = np.zeros((height, width, 3), dtype=np.uint8)
            x, y = (width - resized_width) // 2, (height - resized_height) // 2
            output[y:y + resized_height, x:x + resized_width] = self._cv2.resize(bgr, (resized_width, resized_height))
            if self._enable_zmq:
                ok, buf = self._cv2.imencode(".jpg", output)
                if ok:
                    self._zmq_buffer.write(buf.tobytes())
            if self._enable_bgr_output:
                self._bgr_buffer.write(output)
            self._last_frame_time = time.monotonic()
            self._ready.set()
            return

    def get_jpeg_bytes(self):
        """Return buffered JPEG bytes only while the last frame is fresh."""
        if self._last_frame_time is None or time.monotonic() - self._last_frame_time >= self._receive_timeout:
            return None
        return super().get_jpeg_bytes()

    def get_bgr_frame(self):
        """Return the buffered BGR array only while the last frame is fresh."""
        if self._last_frame_time is None or time.monotonic() - self._last_frame_time >= self._receive_timeout:
            return None
        return super().get_bgr_frame()

    def release(self):
        """Stop reception and close decoder subprocesses, sockets, and the selector."""
        self._closed.set()
        process = self._jpeg_process
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        if self._jpeg_input is not None:
            self._jpeg_input.close()
        if self._socket is not None:
            self._socket.close()
        self._selector.close()
        logger_mp.info(f"[RtpCamera] Released {self._cam_topic}")

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Create an RTP receiver from the native camera mapping."""
        return cls(cam_topic, server_cfg)
