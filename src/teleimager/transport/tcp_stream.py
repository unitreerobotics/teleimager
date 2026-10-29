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
"""Handle App camera control messages and send length-prefixed H264 packets."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import socket
import struct
import threading
import time
from typing import Optional, Tuple

import cv2
import logging_mp
import numpy as np


logger_mp = logging_mp.getLogger(__name__)

OPEN_CAMERA = "OPEN_CAMERA"
CLOSE_CAMERA = "CLOSE_CAMERA"
DEFAULT_STREAM_NAME = "robotree"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 960
DEFAULT_FPS = 30
DEFAULT_BITRATE = 5_000_000

_MAX_COMMAND_BYTES = 64
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_CONTROL_PACKET_BYTES = 4 + _MAX_COMMAND_BYTES + 4 + _MAX_PAYLOAD_BYTES
_CAMERA_REQUEST_HEADER = struct.Struct("<7i")


@dataclass(frozen=True)
class CameraStreamRequest:
    """Store requested dimensions, frame rate, bitrate, stream name, and destination."""

    width: int
    height: int
    fps: int
    bitrate: int
    camera: str
    host: str
    port: int


def parse_camera_stream_request(payload: bytes) -> CameraStreamRequest:
    """Parse the App's version-one camera request payload.

    The payload starts with 0xCAFE and a version byte, followed by seven
    little-endian integers and two one-byte-length UTF-8 strings.

    Raises:
        ValueError: The payload is malformed or contains invalid stream settings.
    """
    if len(payload) < 3 + _CAMERA_REQUEST_HEADER.size:
        raise ValueError("Camera stream request is too short.")
    if payload[:2] != b"\xca\xfe":
        raise ValueError("Camera stream request has invalid magic bytes.")
    if payload[2] != 1:
        raise ValueError(
            f"Unsupported camera stream protocol version: {payload[2]}."
        )

    (
        width,
        height,
        fps,
        bitrate,
        _enable_mv_hevc,
        _render_mode,
        port,
    ) = _CAMERA_REQUEST_HEADER.unpack_from(payload, 3)
    offset = 3 + _CAMERA_REQUEST_HEADER.size
    camera, offset = _read_compact_string(payload, offset)
    host, offset = _read_compact_string(payload, offset)
    if offset != len(payload):
        raise ValueError("Camera stream request contains trailing bytes.")

    if width <= 0 or height <= 0 or fps <= 0 or bitrate <= 0:
        raise ValueError("Camera stream dimensions, FPS, and bitrate must be positive.")
    if not 0 < port <= 65535:
        raise ValueError(f"Invalid camera stream port: {port}.")

    return CameraStreamRequest(
        width=width,
        height=height,
        fps=fps,
        bitrate=bitrate,
        camera=camera,
        host=host,
        port=port,
    )


def compose_camera_mosaic(
    head: np.ndarray,
    left_wrist: np.ndarray,
    right_wrist: np.ndarray,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> np.ndarray:
    """Return a BGR mosaic with the head above the left and right wrists.

    Dimensions are rounded down to even values with a minimum of two pixels.
    The head occupies roughly the upper two thirds; each wrist fills half of
    the remaining strip. Inputs are resized to fill their regions.
    """
    for name, frame in (
        ("head", head),
        ("left wrist", left_wrist),
        ("right wrist", right_wrist),
    ):
        if not isinstance(frame, np.ndarray):
            raise TypeError(f"{name} camera frame must be a numpy array.")
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"{name} camera frame must have three color channels.")

    width = max(2, int(width) & ~1)
    height = max(2, int(height) & ~1)
    head_height = (height * 2 // 3) & ~1
    wrist_height = height - head_height
    half_width = width // 2

    mosaic = np.empty((height, width, 3), dtype=np.uint8)
    mosaic[:head_height] = cv2.resize(head, (width, head_height))
    mosaic[head_height:, :half_width] = cv2.resize(
        left_wrist,
        (half_width, wrist_height),
    )
    mosaic[head_height:, half_width:] = cv2.resize(
        right_wrist,
        (width - half_width, wrist_height),
    )
    return mosaic


def compose_camera_output(
    frames: tuple[Optional[np.ndarray], ...],
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    layout: str = "default",
) -> np.ndarray:
    """Compose BGR camera frames for the requested App layout.

    Args:
        frames: One frame or (head, left wrist, right wrist) for default;
            (left eye, right eye, left wrist, right wrist) for stereo_quad.
        width: Composite output width; a single default frame is returned as is.
        height: Composite output height; a single default frame is returned as is.
        layout: default or stereo_quad. Only stereo_quad accepts missing frames.

    Returns:
        A BGR array. stereo_quad preserves aspect ratio within each tile and
        uses labeled placeholders for None inputs.

    Raises:
        TypeError: A default-layout input is not an array.
        ValueError: The layout, frame count, or image shape is invalid.
    """
    if layout == "stereo_quad":
        if len(frames) != 4:
            raise ValueError("stereo_quad requires left eye, right eye, left wrist, and right wrist.")
        width = max(4, int(width) & ~1)
        height = max(4, int(height) & ~1)
        tile_width, tile_height = width // 2, height // 2
        output = np.zeros((height, width, 3), dtype=np.uint8)
        for index, frame in enumerate(frames):
            x, y = (index % 2) * tile_width, (index // 2) * tile_height
            if frame is None:
                font_scale = min(tile_width / 400, tile_height / 200)
                thickness = max(1, round(font_scale * 2))
                (text_width, text_height), _ = cv2.getTextSize(
                    "nocarema", cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
                )
                cv2.putText(output, "nocarema",
                            (x + (tile_width - text_width) // 2, y + (tile_height + text_height) // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
                continue
            if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
                raise ValueError("camera frame must be a BGR array.")
            scale = min(tile_width / frame.shape[1], tile_height / frame.shape[0])
            resized_width = max(1, round(frame.shape[1] * scale))
            resized_height = max(1, round(frame.shape[0] * scale))
            x += (tile_width - resized_width) // 2
            y += (tile_height - resized_height) // 2
            output[y:y + resized_height, x:x + resized_width] = cv2.resize(frame, (resized_width, resized_height))
        return output
    if layout != "default":
        raise ValueError(f"Unknown App stream layout: {layout}")
    if len(frames) == 1:
        frame = frames[0]
        if not isinstance(frame, np.ndarray):
            raise TypeError("camera frame must be a numpy array.")
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("camera frame must have three color channels.")
        return frame
    if len(frames) == 3:
        return compose_camera_mosaic(
            frames[0],
            frames[1],
            frames[2],
            width=width,
            height=height,
        )
    raise ValueError("App stream requires one camera or three cameras.")


class TcpStreamServer:
    """Serve App control requests and send H264 over a separate TCP connection.

    The App connects to the control listener and requests a destination for
    video. Capture code calls send_frame(); encoding and sending happen in
    that caller while a lock protects the active request and stream state.
    """

    def __init__(
        self,
        *,
        control_host: str = "0.0.0.0",
        control_port: int = 13579,
        stream_name: str = DEFAULT_STREAM_NAME,
        video_config: Optional[dict] = None,
    ) -> None:
        """Prepare control and encoder state without opening sockets.

        Args:
            control_host: Local address for the App control listener.
            control_port: Local control port.
            stream_name: Camera name accepted in OPEN_CAMERA requests.
            video_config: Optional server encoder settings overriding requested
                FPS and bitrate; requested dimensions must match these settings.
        """
        self.control_host = control_host
        self.control_port = int(control_port)
        self.stream_name = stream_name
        self.video_config = dict(video_config) if video_config is not None else None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._listener: Optional[socket.socket] = None
        self._control_client: Optional[socket.socket] = None
        self._control_thread: Optional[threading.Thread] = None
        self._request: Optional[CameraStreamRequest] = None
        self._stream_socket: Optional[socket.socket] = None
        self._codec = None
        self._frame_index = 0
        self._next_frame_time = 0.0
        self._next_connect_time = 0.0

    @property
    def stream_requested(self) -> bool:
        """Return whether an active App request selects a video destination."""
        with self._lock:
            return self._request is not None

    def start(self) -> None:
        """Start the background control listener unless it is already running."""
        if self._control_thread is not None and self._control_thread.is_alive():
            return
        self._stop_event.clear()
        self._control_thread = threading.Thread(
            target=self._serve_control,
            name="teleimager-app-stream-control",
            daemon=True,
        )
        self._control_thread.start()

    def stop(self) -> None:
        """Close sockets, clear the request, and wait briefly for the control worker."""
        self._stop_event.set()
        with self._lock:
            listener = self._listener
            self._listener = None
            control_client = self._control_client
            self._control_client = None
            self._close_stream_locked(clear_request=True)
        for sock in (control_client, listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._control_thread is not None:
            self._control_thread.join(timeout=1.0)
            self._control_thread = None

    def frame_due(self) -> bool:
        """Return whether an active request is due for another frame."""
        with self._lock:
            return (
                self._request is not None
                and time.monotonic() >= self._next_frame_time
            )

    def send_frame(self, bgr_frame: np.ndarray) -> bool:
        """Encode one due BGR frame and send length-prefixed H264 packets.

        Each packet uses a four-byte big-endian length prefix. Stream connection
        or encoding failures retain the request and retry after one second.

        Returns:
            True if encoding completes without error, even if it emits no packet;
            False if not due, no request exists, or connection or sending fails.
        """
        with self._lock:
            request = self._request
            now = time.monotonic()
            if request is None or now < self._next_frame_time:
                return False
            self._next_frame_time = now + 1.0 / request.fps

            if self._stream_socket is None:
                if now < self._next_connect_time:
                    return False
                try:
                    self._connect_stream_locked(request)
                except OSError as exc:
                    self._next_connect_time = now + 1.0
                    logger_mp.warning(
                        "[App Stream] Cannot connect to %s:%d: %s",
                        request.host,
                        request.port,
                        exc,
                    )
                    return False
            try:
                if self._codec is None:
                    self._open_codec_locked(request)
                frame = self._video_frame(bgr_frame, request)
                frame.pts = self._frame_index
                self._frame_index += 1
                for packet in self._codec.encode(frame):
                    payload = bytes(packet)
                    if payload:
                        self._stream_socket.sendall(
                            len(payload).to_bytes(4, "big") + payload
                        )
                return True
            except Exception as exc:
                logger_mp.warning("[App Stream] H264 stream interrupted: %s", exc)
                self._close_stream_locked(clear_request=False)
                self._next_connect_time = now + 1.0
                return False

    def _serve_control(self) -> None:
        """Accept and handle one control connection at a time until shutdown."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.control_host, self.control_port))
        except OSError as exc:
            listener.close()
            logger_mp.error(
                "[App Stream] Cannot listen on %s:%d: %s",
                self.control_host,
                self.control_port,
                exc,
            )
            return
        listener.listen(1)
        listener.settimeout(0.5)
        with self._lock:
            self._listener = listener
        logger_mp.info(
            "[App Stream] Listening for Robotree App on %s:%d.",
            self.control_host,
            self.control_port,
        )

        try:
            while not self._stop_event.is_set():
                try:
                    client, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                client.settimeout(0.5)
                with self._lock:
                    old_client = self._control_client
                    self._control_client = client
                if old_client is not None:
                    try:
                        old_client.close()
                    except OSError:
                        pass
                logger_mp.info("[App Stream] Robotree App connected from %s.", address[0])
                self._handle_control_client(client, address[0])
        finally:
            with self._lock:
                if self._listener is listener:
                    self._listener = None
            try:
                listener.close()
            except OSError:
                pass

    def _handle_control_client(self, client: socket.socket, peer_host: str) -> None:
        """Process OPEN_CAMERA and CLOSE_CAMERA messages for one App connection.

        An empty destination host uses the control peer's IP. Disconnecting the
        active control client also clears its video request and stream socket.
        """
        try:
            while not self._stop_event.is_set():
                message = _receive_control_message(client, self._stop_event)
                if message is None:
                    break
                command, payload = message
                if command == OPEN_CAMERA:
                    request = parse_camera_stream_request(payload)
                    if request.camera != self.stream_name:
                        logger_mp.warning(
                            "[App Stream] Ignoring unsupported stream %r.",
                            request.camera,
                        )
                        continue
                    if self.video_config is not None:
                        width = self.video_config.get("width", DEFAULT_WIDTH)
                        height = self.video_config.get("height", DEFAULT_HEIGHT)
                        if (request.width, request.height) != (width, height):
                            logger_mp.warning(
                                "[App Stream] Requested dimensions %dx%d do not match configured %dx%d.",
                                request.width, request.height, width, height,
                            )
                            continue
                        request = CameraStreamRequest(
                            width=width, height=height,
                            fps=self.video_config.get("fps", DEFAULT_FPS),
                            bitrate=self.video_config.get("bitrate", DEFAULT_BITRATE),
                            camera=request.camera, host=request.host, port=request.port,
                        )
                    if not request.host:
                        request = CameraStreamRequest(
                            width=request.width,
                            height=request.height,
                            fps=request.fps,
                            bitrate=request.bitrate,
                            camera=request.camera,
                            host=peer_host,
                            port=request.port,
                        )
                    with self._lock:
                        self._close_stream_locked(clear_request=True)
                        self._request = request
                    logger_mp.info(
                        "[App Stream] Requested stream %s at %dx%d@%dfps to %s:%d.",
                        request.camera,
                        request.width,
                        request.height,
                        request.fps,
                        request.host,
                        request.port,
                    )
                elif command == CLOSE_CAMERA:
                    with self._lock:
                        self._close_stream_locked(clear_request=True)
                    logger_mp.info("[App Stream] Robotree App stopped the stream.")
                else:
                    logger_mp.warning(
                        "[App Stream] Ignoring unsupported command %r.",
                        command,
                    )
        except (ConnectionError, OSError, ValueError) as exc:
            if not self._stop_event.is_set():
                logger_mp.warning("[App Stream] Control connection closed: %s", exc)
        finally:
            with self._lock:
                if self._control_client is client:
                    self._control_client = None
                    self._close_stream_locked(clear_request=True)
            try:
                client.close()
            except OSError:
                pass

    def _connect_stream_locked(self, request: CameraStreamRequest) -> None:
        """Connect to the video destination; the caller must hold the lock."""
        sock = socket.create_connection((request.host, request.port), timeout=1.0)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
        self._stream_socket = sock
        logger_mp.info(
            "[App Stream] H264 stream connected to %s:%d.",
            request.host,
            request.port,
        )

    def _open_codec_locked(self, request: CameraStreamRequest) -> None:
        """Create the configured libx264 encoder; caller holds the lock."""
        import av

        codec = av.CodecContext.create("libx264", "w")
        codec.width = max(2, request.width & ~1)
        codec.height = max(2, request.height & ~1)
        video = self.video_config or {}
        codec.pix_fmt = video.get("pixel_format", "yuv420p")
        codec.time_base = Fraction(1, request.fps)
        codec.framerate = Fraction(request.fps, 1)
        codec.bit_rate = request.bitrate
        codec.options = {
            "preset": video.get("preset", "ultrafast"),
            "tune": video.get("tune", "zerolatency"),
            "threads": str(video.get("threads", 1)),
            "g": str(video.get("gop_length", request.fps)),
            "keyint_min": str(video.get("gop_length", request.fps)),
            "scenecut": "0",
        }
        codec.open()
        self._codec = codec

    @staticmethod
    def _video_frame(bgr_frame: np.ndarray, request: CameraStreamRequest):
        """Validate BGR input and convert it to even-sized YUV420P for the request."""
        import av

        if not isinstance(bgr_frame, np.ndarray):
            raise TypeError("Camera stream frame must be a numpy array.")
        if bgr_frame.ndim != 3 or bgr_frame.shape[2] != 3:
            raise ValueError("Camera stream frame must have shape (height, width, 3).")
        frame = av.VideoFrame.from_ndarray(
            np.ascontiguousarray(bgr_frame),
            format="bgr24",
        )
        return frame.reformat(
            width=max(2, request.width & ~1),
            height=max(2, request.height & ~1),
            format="yuv420p",
        )

    def _close_stream_locked(self, *, clear_request: bool) -> None:
        """Reset stream state, optionally clearing the request; lock required."""
        sock = self._stream_socket
        self._stream_socket = None
        self._codec = None
        self._frame_index = 0
        self._next_frame_time = 0.0
        self._next_connect_time = 0.0
        if clear_request:
            self._request = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _read_compact_string(payload: bytes, offset: int) -> Tuple[str, int]:
    """Read a one-byte-length UTF-8 string and return it with the next offset."""
    if offset >= len(payload):
        raise ValueError("Camera stream request is missing a string length.")
    length = payload[offset]
    offset += 1
    end = offset + length
    if end > len(payload):
        raise ValueError("Camera stream request contains a truncated string.")
    return payload[offset:end].decode("utf-8"), end


def _receive_control_message(
    sock: socket.socket,
    stop_event: threading.Event,
) -> Optional[Tuple[str, bytes]]:
    """Read one framed App command, or return None if reception ends.

    The outer packet length is big-endian; inner command and payload lengths
    are little-endian. Invalid lengths and truncated packet bodies raise.
    """
    packet_size_bytes = _receive_exact(sock, 4, stop_event)
    if packet_size_bytes is None:
        return None
    packet_size = struct.unpack(">i", packet_size_bytes)[0]
    if not 8 <= packet_size <= _MAX_CONTROL_PACKET_BYTES:
        raise ValueError(f"Invalid control packet length: {packet_size}.")
    packet = _receive_exact(sock, packet_size, stop_event)
    if packet is None:
        raise ConnectionError("Control packet was truncated.")

    command_size = struct.unpack_from("<i", packet)[0]
    if not 0 <= command_size <= _MAX_COMMAND_BYTES:
        raise ValueError(f"Invalid control command length: {command_size}.")
    command_end = 4 + command_size
    payload_size_end = command_end + 4
    if payload_size_end > len(packet):
        raise ConnectionError("Control command was truncated.")
    command_bytes = packet[4:command_end]

    payload_size = struct.unpack_from("<i", packet, command_end)[0]
    if not 0 <= payload_size <= _MAX_PAYLOAD_BYTES:
        raise ValueError(f"Invalid control payload length: {payload_size}.")
    payload = packet[payload_size_end:]
    if len(payload) != payload_size:
        raise ConnectionError("Control payload was truncated.")
    return command_bytes.decode("utf-8"), payload


def _receive_exact(
    sock: socket.socket,
    size: int,
    stop_event: threading.Event,
) -> Optional[bytes]:
    """Read exactly size bytes across timeouts, or return None on EOF or shutdown."""
    data = bytearray()
    while len(data) < size and not stop_event.is_set():
        try:
            chunk = sock.recv(size - len(data))
        except socket.timeout:
            continue
        if not chunk:
            return None
        data.extend(chunk)
    if len(data) != size:
        return None
    return bytes(data)
