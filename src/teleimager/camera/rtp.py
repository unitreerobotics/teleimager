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

"""Receive RTP/H264 or RTP/JPEG video and publish resized camera buffers.

Default: one RTP stream per camera instance (rtp_port), published on its own. Optional, for the R1 head:

- rtp_ports: [left, right] pairs two eye streams into one side-by-side frame (fresh eyes whose reception times
  differ by at most max_pair_delay), like the former RoboTree R1 stereo receiver, so a single binocular topic
  (e.g. head_stereo_camera on ZMQ 55555) keeps working for existing clients.
- jpeg_depay: python reassembles RTP/JPEG in-process. R1 firmware puts a complete JPEG file into each RTP/JPEG
  frame; the GStreamer rtpjpegdepay path truncates it (only the top rows decode).
- rectify: undistorts and stereo-rectifies with the head's own calibration (camera.r1_calib); the rectified
  pinhole parameters are added to this camera's mapping as ``rectified`` (served by the config responder).
"""

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

from ..frame import fit_frame
from .camera import BaseCamera, _turbojpeg

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class _RtpStream:
    """One bound UDP port and its decoder: H264 (jitter buffer), JPEG via GStreamer, or JPEG reassembled here."""

    def __init__(self, port, bind_host, source_host, encoding, jpeg_depay, fps, cv2):
        self.source_host, self.encoding, self.jpeg_depay, self.fps, self._cv2 = source_host, encoding, jpeg_depay, fps, cv2
        self.socket = None
        self.jpeg_input = None
        self.jpeg_process = None
        self.fileobjs = []
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.socket.bind((bind_host, port))
        self.socket.setblocking(False)
        self.fileobjs.append(self.socket)
        if encoding == "JPEG" and jpeg_depay == "python":
            self.jpeg_state = {"ts": None, "parts": {}}
        elif encoding == "JPEG":
            # Filter sources in Python, retaining RTP datagram boundaries for
            # fdsrc. A slow decoder drops packets rather than blocking capture.
            self.jpeg_input, pipeline_input = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            with pipeline_input:
                self.jpeg_input.setblocking(False)
                self.jpeg_input.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                # Both static PT 26 and dynamic PT 96 are used by firmware.
                self.jpeg_process = subprocess.Popen(
                    ["gst-launch-1.0", "-q", "fdsrc", f"fd={pipeline_input.fileno()}",
                     "blocksize=65536", "do-timestamp=true", "!",
                     "application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG",
                     "!", "rtpjitterbuffer", "latency=20", "drop-on-latency=true",
                     "!", "rtpjpegdepay", "!", "jpegparse",
                     "!", "fdsink", "fd=1", "sync=false"],
                    pass_fds=(pipeline_input.fileno(),), stdout=subprocess.PIPE,
                )
            os.set_blocking(self.jpeg_process.stdout.fileno(), False)
            self.decoder = av.CodecContext.create("mjpeg", "r")
            self.decoder.thread_count = 1
            self.fileobjs.append(self.jpeg_process.stdout)
        else:
            self.decoder = H264Decoder()
            self.decoder.codec.thread_count = 1
            self.jitter = JitterBuffer(capacity=128, is_video=True)
            self.reformatter = VideoReformatter()
            self.ssrc = None
            self.decode_frame_time = None

    def port(self):
        return self.socket.getsockname()[1]

    def read(self, fileobj):
        """Drain what is ready on fileobj; return the newest decoded BGR frame, or None.

        Raises OSError when the GStreamer receiver exited.
        """
        bgr = None
        if fileobj is not self.socket:
            data = os.read(fileobj.fileno(), 256 * 1024)
            if not data:
                raise OSError("RTP/JPEG receiver exited; inspect GStreamer stderr.")
            for packet in self.decoder.parse(data):
                decoded = self._cv2.imdecode(np.frombuffer(packet, dtype=np.uint8), self._cv2.IMREAD_COLOR)
                if decoded is not None:
                    bgr = decoded
            return bgr
        # Bound each drain so other streams and JPEG output can run.
        for _ in range(128):
            try:
                data, address = self.socket.recvfrom(65535)
            except BlockingIOError:
                break
            if address[0] != self.source_host:
                continue
            if self.encoding == "JPEG" and self.jpeg_depay == "python":
                from .r1_calib import reassemble_rtp_jpeg
                jpeg = reassemble_rtp_jpeg(data, self.jpeg_state)
                if jpeg is not None:
                    try:
                        bgr = _turbojpeg.decode(jpeg)
                    except Exception:  # noqa: BLE001, S110 - a corrupt JPEG is dropped like a lost frame
                        pass
                continue
            if self.encoding == "JPEG":
                try:
                    self.jpeg_input.send(data)
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
            stalled = self.decode_frame_time is not None and received_at - self.decode_frame_time > max(0.25, 3.0 / self.fps)
            if self.ssrc != packet.ssrc or stalled:
                self.ssrc = packet.ssrc
                self.jitter = JitterBuffer(capacity=128, is_video=True)
                self.decoder = H264Decoder()
                self.decoder.codec.thread_count = 1
                bgr = None
                self.decode_frame_time = received_at
            lost_frame, encoded = self.jitter.add(packet)
            if lost_frame:
                self.decoder = H264Decoder()
                self.decoder.codec.thread_count = 1
                bgr = None
            if encoded is not None:
                self.decode_frame_time = received_at
                for frame in self.decoder.decode(encoded):
                    bgr = self.reformatter.reformat(frame, format="bgr24").to_ndarray()
        return bgr

    def close(self):
        process = self.jpeg_process
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        if self.jpeg_input is not None:
            self.jpeg_input.close()
        if self.socket is not None:
            self.socket.close()


class RtpCamera(BaseCamera):
    """Receive RTP/H264 or RTP/JPEG video without pinning the source port."""

    def __init__(self, cam_topic, server_cfg):
        """Bind the UDP receiver(s) and initialize the selected RTP decoder.

        Args:
            cam_topic: Camera topic used to identify the source.
            server_cfg: Camera mapping with rtp_port (or rtp_ports: [left, right] for a paired side-by-side
                stereo frame), image_shape, and optional bind_host, source_host, receive_timeout,
                rtp_encoding, jpeg_depay ("gstreamer" default, or "python"), max_pair_delay (paired mode,
                seconds, default 0.02) and rectify (see camera.r1_calib; single-stream mode needs eye: left|right).
        """
        import cv2

        super().__init__(cam_topic, server_cfg)
        self._cv2 = cv2
        bind_host = server_cfg.get("bind_host", "0.0.0.0")
        self._source_host = server_cfg.get("source_host", "192.168.123.161")
        ports = server_cfg.get("rtp_ports")
        self._paired = ports is not None
        if self._paired:
            if len(ports) != 2:
                raise ValueError("RTP rtp_ports must list exactly two ports: [left, right].")
        else:
            ports = [server_cfg["rtp_port"]]
        self._receive_timeout = server_cfg.get("receive_timeout", 2.0)
        self._max_pair_delay = server_cfg.get("max_pair_delay", 0.02)
        self._rtp_encoding = server_cfg.get("rtp_encoding", "H264")
        self._jpeg_depay = server_cfg.get("jpeg_depay", "gstreamer")
        height, width = self._img_shape
        if height <= 0 or width <= 0:
            raise ValueError("RTP image_shape requires a positive height and width.")
        if self._paired and width % 2:
            raise ValueError("RTP paired image_shape requires an even width.")
        if self._receive_timeout <= 0:
            raise ValueError("RTP receive_timeout must be positive.")
        if self._paired and not 0 < self._max_pair_delay < self._receive_timeout:
            raise ValueError("RTP pairing requires 0 < max_pair_delay < receive_timeout.")
        if self._rtp_encoding not in ("H264", "JPEG"):
            raise ValueError("RTP rtp_encoding must be H264 or JPEG.")
        if self._jpeg_depay not in ("gstreamer", "python"):
            raise ValueError("RTP jpeg_depay must be gstreamer or python.")
        self._selector = selectors.DefaultSelector()
        self._streams = []
        self._closed = threading.Event()
        self._last_frame_time = None
        self._source_shape = None
        self._pending = [None] * len(ports)
        # Paired mode: one worker thread per eye receives, decodes and rectifies (OpenCV and TurboJPEG release
        # the GIL, so both eyes run in parallel); the capture thread only pairs, stitches and encodes.
        self._eye_cond = threading.Condition()
        self._eye_latest = [None] * len(ports)      # (tile, decoded source, reception time, sequence number)
        self._eye_used = [0] * len(ports)
        self._eye_error = None
        self._eye_workers = []
        try:
            self._rect_maps = self._make_rect_maps(server_cfg)
            for eye, port in enumerate(ports):
                stream = _RtpStream(port, bind_host, self._source_host, self._rtp_encoding, self._jpeg_depay,
                                    self._fps, cv2)
                self._streams.append(stream)
                if not self._paired:
                    for fileobj in stream.fileobjs:
                        self._selector.register(fileobj, selectors.EVENT_READ, eye)
            if self._paired:
                for eye in range(len(self._streams)):
                    worker = threading.Thread(target=self._eye_worker, args=(eye,), daemon=True,
                                              name=f"{cam_topic}-eye{eye}")
                    self._eye_workers.append(worker)
                    worker.start()
        except Exception:
            self.release()
            raise
        logger_mp.info(str(self))

    def _make_rect_maps(self, server_cfg):
        """Remap tables per received stream from the rectify mapping, or None when rectification is off."""
        cfg = server_cfg.get("rectify")
        if not cfg:
            return None
        from .r1_calib import load_head_params, rectify_maps
        height, width = self._img_shape
        out = (width // 2, height) if self._paired else (width, height)
        if "left" in cfg and "right" in cfg:                   # parameters written inline in the config
            size = list(cfg.get("raw_size", (1280, 1088)))
            params = {"left": dict(cfg["left"], size=size), "right": dict(cfg["right"], size=size),
                      "R": cfg["R"], "T": cfg["T"], "source": "inline config", "origin": "config"}
        else:
            params = load_head_params(cfg, log=logger_mp.warning)
        maps, info = rectify_maps(params, out, cfg.get("alpha", 0.0), cfg.get("right_dy", 0.0))
        if not self._paired:
            eye = cfg.get("eye")
            if eye not in ("left", "right"):
                raise ValueError("RTP rectify on a single stream requires eye: left or right.")
            maps = [maps[0 if eye == "left" else 1]]
            info["eye"] = eye
        server_cfg["rectified"] = info
        logger_mp.info(f"[RtpCamera: {self._cam_topic}] rectifying ({info['source']}, from {info['origin']}): "
                       f"f {info['fx']:.1f} px, cx {info['cx']:.1f}, cy {info['cy']:.1f}, "
                       f"baseline {info['baseline_m'] * 1000:.1f} mm, eye {out[0]}x{out[1]}")
        return maps

    def __str__(self):
        ports = ", ".join(str(stream.port()) for stream in self._streams)
        mode = "paired left/right ports" if self._paired else "port"
        return (f"[RtpCamera: {self._cam_topic}] RTP/{self._rtp_encoding} {mode}={ports}, "
                f"output={self._img_shape[1]}x{self._img_shape[0]} @ {self._fps} FPS")

    def _tile(self, eye, bgr):
        """Rectify one decoded eye, or fit it into its tile with black borders."""
        if self._rect_maps is not None:
            return self._cv2.remap(bgr, self._rect_maps[eye][0], self._rect_maps[eye][1], self._cv2.INTER_LINEAR)
        height, width = self._img_shape
        tile_width = width // len(self._streams)
        scale = min(tile_width / bgr.shape[1], height / bgr.shape[0])
        resized_width = max(1, round(bgr.shape[1] * scale))
        resized_height = max(1, round(bgr.shape[0] * scale))
        tile = np.zeros((height, tile_width, 3), dtype=np.uint8)
        x, y = (tile_width - resized_width) // 2, (height - resized_height) // 2
        tile[y:y + resized_height, x:x + resized_width] = self._cv2.resize(bgr, (resized_width, resized_height))
        return tile

    def _eye_worker(self, eye):
        """Paired mode: receive, decode and tile one eye until release; keep only its newest frame."""
        stream, selector, seq = self._streams[eye], selectors.DefaultSelector(), 0
        try:
            for fileobj in stream.fileobjs:
                selector.register(fileobj, selectors.EVENT_READ)
            while not self._closed.is_set():
                for key, _ in selector.select(0.05):
                    bgr = stream.read(key.fileobj)
                    if bgr is None:
                        continue
                    received_at = time.monotonic()
                    tile = self._tile(eye, bgr)
                    seq += 1
                    with self._eye_cond:
                        self._eye_latest[eye] = (tile, bgr, received_at, seq)
                        self._eye_cond.notify_all()
        except (OSError, ValueError) as exc:
            if not self._closed.is_set():
                with self._eye_cond:
                    self._eye_error = exc
                    self._eye_cond.notify_all()
        finally:
            selector.close()

    def _update_paired(self):
        """Wait for a fresh left/right pair from the eye workers and publish it side by side."""
        started_at = time.monotonic()
        with self._eye_cond:
            while not self._closed.is_set():
                if self._eye_error is not None:
                    raise self._eye_error
                remaining = self._receive_timeout - (time.monotonic() - started_at)
                if remaining <= 0:
                    self._ready.clear()
                    raise TimeoutError(f"RTP {self._cam_topic} received no fresh pair for {self._receive_timeout}s")
                fresh = [e if e is not None and e[3] > used else None
                         for e, used in zip(self._eye_latest, self._eye_used)]
                if None in fresh:
                    self._eye_cond.wait(min(remaining, 0.05))
                    continue
                (tile_l, src_l, t_left, seq_l), (tile_r, src_r, t_right, seq_r) = fresh
                # Independent RTP clocks have different origins: pair by reception
                # time and wait for the partner of the newer eye.
                if abs(t_left - t_right) > self._max_pair_delay:
                    older = 0 if t_left < t_right else 1
                    self._eye_used[older] = fresh[older][3]
                    continue
                self._eye_used = [seq_l, seq_r]
                break
            else:
                return
        output = np.hstack([tile_l, tile_r])
        source = output
        if self._rect_maps is None and self._display_shape is not None:
            source = np.hstack([src_l, fit_frame(src_r, src_l.shape[1], src_l.shape[0])])
        self._publish(output, source)

    def _update_frame(self):
        """Wait for a decoded frame (or a fresh left/right pair), then publish it.

        Filters datagrams by source IP. H264 uses a jitter buffer; JPEG uses a
        GStreamer depayloader or the in-process reassembler. Missing frames clear
        readiness and raise TimeoutError after receive_timeout seconds; decoded
        output is rectified when configured, otherwise padded with black borders.
        """
        if self._paired:
            return self._update_paired()
        started_at = time.monotonic()
        while not self._closed.is_set():
            remaining = self._receive_timeout - (time.monotonic() - started_at)
            if remaining <= 0:
                self._ready.clear()
                raise TimeoutError(f"RTP {self._cam_topic} received no fresh frame for {self._receive_timeout}s")
            try:
                for key, _ in self._selector.select(min(remaining, 0.05)):
                    bgr = self._streams[key.data].read(key.fileobj)
                    if bgr is not None:
                        self._pending[key.data] = (bgr, time.monotonic())
            except (OSError, ValueError):
                if self._closed.is_set():
                    return
                raise
            if self._pending[0] is None:
                continue
            bgr = self._pending[0][0]
            self._pending = [None]
            self._publish(self._tile(0, bgr), bgr)
            return

    def _publish(self, output, source):
        """Fill the enabled buffers with the output frame; source feeds the App display buffer."""
        self._source_shape = source.shape[:2]
        display_shape = self._display_shape
        if display_shape is not None:
            # Fit the decoded frame once here, in this camera's thread, so
            # the App stream only copies it into place.
            self._display_buffer.write(fit_frame(source, display_shape[1], display_shape[0]))
        if self._enable_color_zmq:
            ok, buf = self._cv2.imencode(".jpg", output)
            if ok:
                self._color_zmq_buffer.write(buf.tobytes())
        if self._webrtc_connected.is_set():
            self._webrtc_buffer.write(av.VideoFrame.from_ndarray(output, format="bgr24"))
        if self._enable_bgr_output:
            self._bgr_buffer.write(output)
        self._last_frame_time = time.monotonic()
        self._ready.set()

    def get_bgr_frame(self):
        """Return the buffered BGR array only while the last frame is fresh."""
        if self._last_frame_time is None or time.monotonic() - self._last_frame_time >= self._receive_timeout:
            return None
        return super().get_bgr_frame()

    def get_display_frame(self):
        """Return the display frame fitted from the decoded source while fresh."""
        if self._display_shape is None or self._display_buffer is None:
            return None
        if self._last_frame_time is None or time.monotonic() - self._last_frame_time >= self._receive_timeout:
            return None
        frame = self._display_buffer.read()
        if frame is None or frame.shape[:2] != self._display_shape:
            # The shape changed since the last capture; wait for the next one.
            return None
        return frame

    def get_source_shape(self):
        """Return the decoded (height, width) of the RTP source, or None before a frame."""
        return self._source_shape

    def release(self):
        """Stop reception and close decoder subprocesses, sockets, and the selector."""
        self._closed.set()
        with self._eye_cond:
            self._eye_cond.notify_all()
        for worker in self._eye_workers:
            worker.join(timeout=1.0)
        for stream in self._streams:
            stream.close()
        self._selector.close()
        logger_mp.info(f"[RtpCamera] Released {self._cam_topic}")

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Create an RTP receiver from the native camera mapping."""
        return cls(cam_topic, server_cfg)
