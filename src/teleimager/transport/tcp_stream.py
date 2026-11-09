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
"""Handle App camera control, send length-prefixed H264, and relay teleop status."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import math
import select
import socket
import struct
import threading
import time
from typing import Optional, Tuple

import cv2
import logging_mp
import numpy as np

from ..frame import fit_frame


logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)

OPEN_CAMERA = "OPEN_CAMERA"
CLOSE_CAMERA = "CLOSE_CAMERA"
SUBSCRIBE_TELEOP_STATUS = "SUBSCRIBE_TELEOP_STATUS"
DEFAULT_STREAM_NAME = "robotree"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 960
DEFAULT_FPS = 30
DEFAULT_BITRATE = 5_000_000
DEFAULT_STATUS_PORT = 13580
APP_STATUS_PORT = 13581

# Protocol version two: the App sends OPEN_STREAM with JSON, and each video
# connection starts with a stream-info message before any H264 data.
OPEN_STREAM = "OPEN_STREAM"
REQUEST_KEYFRAME = "REQUEST_KEYFRAME"
STREAM_INFO_MAGIC = b"RTSI"
STREAM_PROTOCOL_VERSION = 2
RENDER_MODES = ("immersive", "window")
DEFAULT_EYE_SHAPE = (960, 1280)
DEFAULT_MAX_BITRATE = 20_000_000
DEFAULT_MIN_BITRATE = 4_000_000
DEFAULT_VBV_SECONDS = 0.04
DEFAULT_PRESET = "superfast"
DEFAULT_THREADS = 4
SUPPORTED_CODECS = ("h264",)
HEAD_ROLES = ("head_left", "head_right")
QUAD_ROLES = ("head_left", "head_right", "wrist_left", "wrist_right")

_MAX_COMMAND_BYTES = 64
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_CONTROL_PACKET_BYTES = 4 + _MAX_COMMAND_BYTES + 4 + _MAX_PAYLOAD_BYTES
_MAX_STATUS_BYTES = 4096
_CAMERA_REQUEST_HEADER = struct.Struct("<7i")
# Linux TCP_NOTSENT_LOWAT; the socket module does not export the constant.
_TCP_NOTSENT_LOWAT = getattr(socket, "TCP_NOTSENT_LOWAT", 25)
_KEYFRAME_MIN_INTERVAL = 0.5  # seconds between honored keyframe requests
_STALL_TIMEOUT = 5.0  # seconds without send progress before reconnecting
_STATS_INTERVAL = 5.0  # seconds between stream statistics log lines
_ALIGNMENT = 16  # H264 macroblock size; avoids decoder padding rows


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


def is_teleop_status(payload: object) -> bool:
    """Return whether payload is a version-one teleop status within relay limits.

    Limits: state 1-64 characters, sequence a positive integer, session_id up
    to 128, message up to 256, at most three details of up to 256 characters,
    reason up to 64, and at most three hints. A hint has a code of up to 64
    characters and either finite non-negative centimeters with a positive
    limit_centimeters, or integer degrees from 0 to 180.
    """
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "robotree_teleop_status"
        or type(payload.get("version")) is not int
        or payload["version"] != 1
        or not isinstance(payload.get("state"), str)
        or not 0 < len(payload["state"]) <= 64
        or type(payload.get("sequence")) is not int
        or payload["sequence"] <= 0
    ):
        return False
    session = payload.get("session_id", "")
    message = payload.get("message", "")
    details = payload.get("details", [])
    reason = payload.get("reason", "")
    hints = payload.get("hints", [])
    return (
        isinstance(session, str) and len(session) <= 128
        and isinstance(message, str) and len(message) <= 256
        and isinstance(details, list) and len(details) <= 3
        and all(isinstance(line, str) and len(line) <= 256 for line in details)
        and isinstance(reason, str) and len(reason) <= 64
        and isinstance(hints, list) and len(hints) <= 3
        and all(_is_status_hint(hint) for hint in hints)
    )


def _is_status_hint(hint: object) -> bool:
    """Return whether hint is one valid position or angle deviation."""
    if not isinstance(hint, dict) or not isinstance(hint.get("code"), str) or len(hint["code"]) > 64:
        return False
    if "centimeters" in hint:
        return all(
            type(hint.get(field)) in (int, float) and math.isfinite(hint[field])
            for field in ("centimeters", "limit_centimeters")
        ) and hint["centimeters"] >= 0 and hint["limit_centimeters"] > 0
    return type(hint.get("degrees")) is int and 0 <= hint["degrees"] <= 180


@dataclass(frozen=True)
class StreamRequest:
    """Store one App stream request in protocol-independent form.

    Attributes:
        version: 1 for OPEN_CAMERA, 2 for OPEN_STREAM.
        request_id: App request counter; 0 for version one.
        camera: Requested stream name.
        host: Video destination host; empty selects the control peer.
        port: Video destination TCP port on the App.
        render_mode: ``immersive`` or ``window``; version one is ``window``.
        decoders: (codec, max_width, max_height) the App can decode.
        width: Version-one requested width; 0 for version two.
        height: Version-one requested height; 0 for version two.
    """

    version: int
    request_id: int
    camera: str
    host: str
    port: int
    render_mode: str
    decoders: Tuple[Tuple[str, int, int], ...]
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class StreamGeometry:
    """Describe the encoded frame and where each camera role sits in it.

    Attributes:
        codec: Encoder codec name, currently ``h264``.
        width: Encoded width in pixels.
        height: Encoded height in pixels.
        fps: Nominal frame rate.
        layout: ``stereo`` (both eyes side by side) or ``quad`` (2x2 grid).
        rects: (role, (x, y, width, height)) with a top-left origin.
        bitrate: Bitrate budget in bits per second.
    """

    codec: str
    width: int
    height: int
    fps: int
    layout: str
    rects: Tuple[Tuple[str, Tuple[int, int, int, int]], ...]
    bitrate: int

    def rect(self, role: str) -> Optional[Tuple[int, int, int, int]]:
        """Return the pixel rectangle of role, or None if the role is absent."""
        for name, rect in self.rects:
            if name == role:
                return rect
        return None

    def stream_info(self, request_id: int) -> bytes:
        """Return the RTSI message that starts a version-two video connection."""
        info = {
            "version": STREAM_PROTOCOL_VERSION,
            "request_id": request_id,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "bitrate": self.bitrate,
            "layout": self.layout,
            "rects": {role: list(rect) for role, rect in self.rects},
        }
        return STREAM_INFO_MAGIC + json.dumps(info, separators=(",", ":")).encode("utf-8")


def parse_stream_request(payload: bytes) -> StreamRequest:
    """Parse a version-two OPEN_STREAM JSON payload.

    Raises:
        ValueError: The payload is not valid JSON or a field is invalid.
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"OPEN_STREAM payload is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("OPEN_STREAM payload must be a JSON object.")
    if data.get("version") != STREAM_PROTOCOL_VERSION:
        raise ValueError(f"Unsupported OPEN_STREAM version: {data.get('version')!r}.")
    request_id = data.get("request_id")
    if type(request_id) is not int or request_id < 0:
        raise ValueError("OPEN_STREAM request_id must be a non-negative integer.")
    camera = data.get("stream_name")
    if not isinstance(camera, str) or not camera:
        raise ValueError("OPEN_STREAM stream_name must be a non-empty string.")
    render_mode = data.get("render_mode")
    if render_mode not in RENDER_MODES:
        raise ValueError(f"OPEN_STREAM render_mode must be one of {RENDER_MODES!r}.")
    host = data.get("host", "")
    if not isinstance(host, str):
        raise ValueError("OPEN_STREAM host must be a string.")
    port = data.get("port")
    if type(port) is not int or not 0 < port <= 65535:
        raise ValueError(f"Invalid OPEN_STREAM port: {port!r}.")
    decoders = data.get("decoders")
    if not isinstance(decoders, dict) or not decoders:
        raise ValueError("OPEN_STREAM decoders must map codecs to size limits.")
    parsed = []
    for codec, limits in decoders.items():
        if not isinstance(codec, str) or not isinstance(limits, dict):
            raise ValueError("OPEN_STREAM decoders must map codec names to objects.")
        max_width, max_height = limits.get("max_width"), limits.get("max_height")
        if type(max_width) is not int or type(max_height) is not int or max_width < _ALIGNMENT or max_height < _ALIGNMENT:
            raise ValueError(f"OPEN_STREAM decoder {codec!r} needs positive max_width and max_height.")
        parsed.append((codec, max_width, max_height))
    return StreamRequest(
        version=STREAM_PROTOCOL_VERSION,
        request_id=request_id,
        camera=camera,
        host=host,
        port=port,
        render_mode=render_mode,
        decoders=tuple(parsed),
    )


def select_stream_geometry(
    request: StreamRequest,
    *,
    fps: int,
    quad_shape: Tuple[int, int],
    quad_bitrate: int,
    eye_shape: Tuple[int, int],
    stereo_bitrate: int,
    eye_source_shape: Optional[Tuple[int, int]],
    codecs: Tuple[str, ...] = SUPPORTED_CODECS,
) -> Optional[StreamGeometry]:
    """Choose the encoded frame for a request; return None if no codec matches.

    Window mode and version one use the configured 2x2 quad. Immersive mode
    places both eyes side by side; each eye is the smallest of the eye source,
    the configured eye shape and half the App decoder width (full height),
    rounded down to whole 16-pixel blocks.

    Args:
        request: Parsed App request.
        fps: Nominal stream frame rate.
        quad_shape: (height, width) of the quad layout.
        quad_bitrate: Bitrate budget of the quad layout.
        eye_shape: (height, width) upper limit of one eye.
        stereo_bitrate: Bitrate budget of the stereo layout.
        eye_source_shape: (height, width) of one decoded head eye, or None if
            unknown; unknown sources use the configured eye shape.
        codecs: Server codecs in preference order.
    """
    limits = {codec: (width, height) for codec, width, height in request.decoders}
    codec = next((name for name in codecs if name in limits), None)
    if codec is None:
        return None
    max_width, max_height = limits[codec]
    if request.version == 1 or request.render_mode == "window":
        height, width = quad_shape
        if request.version == 1:
            height, width = request.height, request.width
        width, height = max(2, width & ~1), max(2, height & ~1)
        tile_width, tile_height = width // 2, height // 2
        rects = tuple(
            (role, ((index % 2) * tile_width, (index // 2) * tile_height, tile_width, tile_height))
            for index, role in enumerate(QUAD_ROLES)
        )
        return StreamGeometry(codec=codec, width=width, height=height, fps=fps,
                              layout="quad", rects=rects, bitrate=quad_bitrate)
    eye_height, eye_width = eye_shape
    if eye_source_shape is not None:
        eye_height = min(eye_height, eye_source_shape[0])
        eye_width = min(eye_width, eye_source_shape[1])
    eye_width = min(eye_width, max_width // 2)
    eye_height = min(eye_height, max_height)
    eye_width = max(_ALIGNMENT, eye_width - eye_width % _ALIGNMENT)
    eye_height = max(_ALIGNMENT, eye_height - eye_height % _ALIGNMENT)
    rects = (
        ("head_left", (0, 0, eye_width, eye_height)),
        ("head_right", (eye_width, 0, eye_width, eye_height)),
    )
    return StreamGeometry(codec=codec, width=2 * eye_width, height=eye_height, fps=fps,
                          layout="stereo", rects=rects, bitrate=stereo_bitrate)


def compose_stream_frame(
    frames_by_role: dict,
    geometry: StreamGeometry,
) -> np.ndarray:
    """Place each role's BGR frame into its rectangle of the encoded frame.

    Frames that already match their rectangle are copied without resampling;
    others are fitted. Missing roles get a labeled placeholder.
    """
    output = np.zeros((geometry.height, geometry.width, 3), dtype=np.uint8)
    for role, (x, y, width, height) in geometry.rects:
        frame = frames_by_role.get(role)
        if frame is None:
            _draw_placeholder(output, x, y, width, height)
            continue
        if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("camera frame must be a BGR array.")
        output[y:y + height, x:x + width] = fit_frame(frame, width, height)
    return output


def _draw_placeholder(output: np.ndarray, x: int, y: int, width: int, height: int) -> None:
    """Write the "nocarema" label centered in one black tile."""
    font_scale = min(width / 400, height / 200)
    thickness = max(1, round(font_scale * 2))
    (text_width, text_height), _ = cv2.getTextSize(
        "nocarema", cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
    )
    cv2.putText(output, "nocarema",
                (x + (width - text_width) // 2, y + (height + text_height) // 2),
                cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)


class BitrateController:
    """Adapt the target bitrate to the link from send-backlog observations.

    Congestion (a frame skipped because the previous one is still unsent)
    lowers the target by ``decrease_factor`` at most every
    ``decrease_interval`` seconds. After ``recover_delay`` seconds without
    congestion the target rises by ``increase_factor`` at most every
    ``increase_interval`` seconds. The target stays within [minimum, maximum].
    """

    def __init__(
        self,
        *,
        maximum: int,
        minimum: int,
        now: float,
        decrease_factor: float = 0.75,
        increase_factor: float = 1.1,
        decrease_interval: float = 2.0,
        increase_interval: float = 2.0,
        recover_delay: float = 4.0,
    ) -> None:
        if not 0 < minimum <= maximum:
            raise ValueError("Bitrate limits must satisfy 0 < minimum <= maximum.")
        self.maximum = int(maximum)
        self.minimum = int(minimum)
        self.target = int(maximum)
        self.decrease_factor = decrease_factor
        self.increase_factor = increase_factor
        self.decrease_interval = decrease_interval
        self.increase_interval = increase_interval
        self.recover_delay = recover_delay
        self._last_change = now
        self._last_congestion = float("-inf")

    def update(self, *, congested: bool, now: float) -> int:
        """Record one observation and return the possibly changed target."""
        if congested:
            self._last_congestion = now
            if now - self._last_change >= self.decrease_interval and self.target > self.minimum:
                self.target = max(self.minimum, int(self.target * self.decrease_factor))
                self._last_change = now
        elif (
            now - self._last_congestion >= self.recover_delay
            and now - self._last_change >= self.increase_interval
            and self.target < self.maximum
        ):
            self.target = min(self.maximum, int(self.target * self.increase_factor))
            self._last_change = now
        return self.target


class TcpStreamServer:
    """Serve App control requests and stream H264 over a separate TCP connection.

    The control thread only records requests. OPEN_CAMERA (version one) gets
    the configured 2x2 quad. OPEN_STREAM (version two) gets a geometry chosen
    from the render mode, the App decoder limits and the head camera size,
    announced by an RTSI message at the start of each video connection.

    One stream thread, the caller of pump() and send_frame(), owns the encoder
    and the non-blocking video socket; it never holds the lock while it
    connects, encodes or sends. A frame is skipped before encoding while the
    previous one is still unsent, and sustained skips lower the bitrate
    through BitrateController instead of tearing the connection down. A
    connection that makes no progress for five seconds is reopened.

    A UDP receiver accepts ``robotree_teleop_status`` from RoboTree Policy,
    answers each accepted status with ``robotree_overlay_status_ack``, and
    forwards it to the App's UDP port 13581 once the App sends
    SUBSCRIBE_TELEOP_STATUS; the App draws the status itself. The video is
    never modified. The receiver trusts every sender that can reach
    status_host.
    """

    def __init__(
        self,
        *,
        control_host: str = "0.0.0.0",
        control_port: int = 13579,
        stream_name: str = DEFAULT_STREAM_NAME,
        video_config: Optional[dict] = None,
        status_host: str = "0.0.0.0",
        status_port: Optional[int] = DEFAULT_STATUS_PORT,
        status_timeout: float = 3.0,
        stereo_enabled: bool = False,
        eye_source_shape=None,
    ) -> None:
        """Prepare control, encoder, and status state without opening sockets.

        Args:
            control_host: Local address for the App control listener.
            control_port: Local control port.
            stream_name: Camera name accepted in App requests.
            video_config: Optional App stream settings (the ``app_stream``
                mapping); version-one requests must match its width and height.
            status_host: Local address for the teleop status receiver.
            status_port: UDP port for teleop status, or None to disable it.
            status_timeout: Seconds after which a new status session may
                replace the current one with any sequence.
            stereo_enabled: Accept version-two OPEN_STREAM requests; the
                server enables it when head camera roles are configured.
            eye_source_shape: Callable returning the (height, width) of one
                decoded head eye, or None while unknown.
        """
        self.control_host = control_host
        self.control_port = int(control_port)
        self.stream_name = stream_name
        self.video_config = dict(video_config) if video_config is not None else None
        video = self.video_config or {}
        self.fps = int(video.get("fps", DEFAULT_FPS))
        self.quad_shape = (int(video.get("height", DEFAULT_HEIGHT)), int(video.get("width", DEFAULT_WIDTH)))
        self.quad_bitrate = int(video.get("bitrate", DEFAULT_BITRATE))
        self.stereo_bitrate = int(video.get("max_bitrate", DEFAULT_MAX_BITRATE))
        self.min_bitrate = int(video.get("min_bitrate", DEFAULT_MIN_BITRATE))
        self.eye_shape = tuple(int(value) for value in video.get("eye_shape", DEFAULT_EYE_SHAPE))
        self.codecs = tuple(video.get("codecs", SUPPORTED_CODECS))
        self.stereo_enabled = stereo_enabled
        self._eye_source_shape = eye_source_shape or (lambda: None)
        self.status_host = status_host
        self.status_port = None if status_port is None else int(status_port)
        self.status_timeout = float(status_timeout)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._listener: Optional[socket.socket] = None
        self._control_client: Optional[socket.socket] = None
        self._control_thread: Optional[threading.Thread] = None
        # Control state, guarded by the lock.
        self._request: Optional[StreamRequest] = None
        self._geometry: Optional[StreamGeometry] = None
        self._generation = 0
        self._keyframe_requested = False
        self._last_keyframe_request = float("-inf")
        # Stream state, owned by the stream thread.
        self._stream_socket: Optional[socket.socket] = None
        self._codec = None
        self._pending = bytearray()
        self._active_generation: Optional[int] = None
        self._next_connect_time = 0.0
        self._controller: Optional[BitrateController] = None
        self._stream_started_at = 0.0
        self._last_pts = -1
        self._last_progress = 0.0
        self._frames_encoded = 0
        self._stats_started_at = 0.0
        self._stats_bytes = 0
        self._stats_encoded = 0
        self._stats_skipped = 0
        self._stats_encode_ms: list = []
        self._status_socket: Optional[socket.socket] = None
        self._status_thread: Optional[threading.Thread] = None
        # (payload, (sender, session_id), monotonic receive time) of the
        # latest accepted status, and the sessions it replaced.
        self._teleop_status: Optional[tuple] = None
        self._retired_status_sessions: list = []
        # (App host, UDP socket bound to the control connection's local IP).
        self._status_subscriber: Optional[Tuple[str, socket.socket]] = None

    @property
    def stream_requested(self) -> bool:
        """Return whether an active App request selects a video destination."""
        with self._lock:
            return self._request is not None

    @property
    def stream_active(self) -> bool:
        """Return whether H264 is connected to the App's requested destination."""
        with self._lock:
            return self._request is not None and self._stream_socket is not None

    def snapshot(self) -> Tuple[int, Optional[StreamRequest], Optional[StreamGeometry]]:
        """Return the request generation, the active request and its geometry."""
        with self._lock:
            return self._generation, self._request, self._geometry

    def start(self) -> None:
        """Start the control listener and status receiver unless already running."""
        if self._control_thread is not None and self._control_thread.is_alive():
            return
        self._stop_event.clear()
        self._control_thread = threading.Thread(
            target=self._serve_control,
            name="teleimager-app-stream-control",
            daemon=True,
        )
        self._control_thread.start()
        if self.status_port is not None:
            self._status_thread = threading.Thread(
                target=self._serve_status,
                name="teleimager-teleop-status",
                daemon=True,
            )
            self._status_thread.start()

    def stop(self) -> None:
        """Close sockets, clear the request, and wait briefly for the workers."""
        self._stop_event.set()
        with self._lock:
            listener = self._listener
            self._listener = None
            control_client = self._control_client
            self._control_client = None
            status_socket = self._status_socket
            self._status_socket = None
            self._set_request_locked(None, None)
        self._replace_status_subscriber(None)
        self._reset_stream()
        for sock in (control_client, listener, status_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for thread in (self._control_thread, self._status_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        self._control_thread = None
        self._status_thread = None

    def pump(self) -> bool:
        """Advance the stream without a new frame: follow requests, connect, flush.

        Call from the stream thread whenever it is idle.

        Returns:
            True if the stream is connected and has no unsent bytes.
        """
        now = time.monotonic()
        state = self._step(now)
        self._log_stats(now)
        return state is not None and self._stream_socket is not None and not self._pending

    def send_frame(self, bgr_frame: np.ndarray, generation: Optional[int] = None) -> bool:
        """Encode one BGR frame and queue length-prefixed H264 packets.

        The frame is skipped before encoding while the previous frame is still
        unsent or the socket is not writable; skips feed the bitrate controller.
        Encoding or connection failures keep the request and reconnect after
        one second.

        Args:
            bgr_frame: Frame composed for the active geometry.
            generation: Request generation the frame was composed for; a
                frame for an older request is dropped.

        Returns:
            True if the frame was encoded, False if it was skipped or failed.
        """
        now = time.monotonic()
        state = self._step(now)
        if state is None:
            return False
        current_generation, request, geometry, force_keyframe = state
        if generation is not None and generation != current_generation:
            return False
        if self._stream_socket is None:
            return False
        congested = bool(self._pending) or not self._writable()
        target = self._controller.update(congested=congested, now=now)
        if congested:
            self._stats_skipped += 1
            self._log_stats(now)
            return False
        try:
            if self._codec is None:
                self._codec = self._open_codec(geometry, target)
            elif self._codec.bit_rate != target:
                self._codec.bit_rate = target
            frame = self._video_frame(bgr_frame, geometry)
            frame.pts = self._next_pts(now)
            if force_keyframe or self._frames_encoded == 0:
                frame.pict_type = _intra_picture_type()
            started = time.perf_counter()
            packets = self._codec.encode(frame)
            self._stats_encode_ms.append((time.perf_counter() - started) * 1000.0)
            for packet in packets:
                payload = bytes(packet)
                if payload:
                    self._pending += len(payload).to_bytes(4, "big") + payload
            self._frames_encoded += 1
            self._stats_encoded += 1
            if force_keyframe:
                with self._lock:
                    if self._generation == current_generation:
                        self._keyframe_requested = False
            self._flush(now)
        except Exception as exc:
            logger_mp.warning("[App Stream] H264 stream interrupted: %s", exc)
            self._reset_stream()
            self._next_connect_time = now + 1.0
            return False
        self._log_stats(now)
        return True

    def accept_teleop_status(self, payload: object, sender: tuple, *, now: float) -> bool:
        """Store a valid status in sender and session order; return whether to ACK.

        A session is the sender address with its session_id. Within a session,
        a lower sequence is dropped and an equal one is a resend: it is
        acknowledged and forwarded but does not refresh the receive time.
        A new session replaces a fresh one only from sequence 1, and any
        sequence once the previous status is older than status_timeout. The
        16 most recently replaced sessions stay rejected.
        """
        if not is_teleop_status(payload):
            return False
        key = (sender, payload.get("session_id", ""))
        with self._lock:
            if key in self._retired_status_sessions:
                return False
            if self._teleop_status is not None:
                old_payload, old_key, received_at = self._teleop_status
                if key == old_key:
                    if payload["sequence"] <= old_payload["sequence"]:
                        return payload["sequence"] == old_payload["sequence"]
                elif now - received_at <= self.status_timeout and payload["sequence"] != 1:
                    return False
                else:
                    self._retired_status_sessions = [*self._retired_status_sessions, old_key][-16:]
            self._teleop_status = (dict(payload), key, now)
        return True

    def _serve_status(self) -> None:
        """Receive teleop status, forward it to a subscribed App, and ACK it.

        Undecodable or invalid datagrams get no reply. The ACK reports whether
        the App video stream is connected and echoes session_id when present.
        """
        status_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            status_socket.bind((self.status_host, self.status_port))
            status_socket.settimeout(0.2)
            with self._lock:
                self._status_socket = status_socket
            logger_mp.info("[App Stream] Teleop status on %s:%d.", *status_socket.getsockname())
            while not self._stop_event.is_set():
                try:
                    data, sender = status_socket.recvfrom(_MAX_STATUS_BYTES)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    payload = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not self.accept_teleop_status(payload, sender, now=time.monotonic()):
                    continue
                with self._lock:
                    stream_active = self.stream_active
                    subscriber = self._status_subscriber
                if subscriber is not None:
                    app_host, forward_socket = subscriber
                    try:
                        forward_socket.sendto(data, (app_host, APP_STATUS_PORT))
                    except OSError as exc:
                        logger_mp.warning("[App Stream] Cannot forward teleop status: %s", exc)
                ack = {
                    "type": "robotree_overlay_status_ack",
                    "version": 1,
                    "status_type": "robotree_teleop_status",
                    "sequence": payload["sequence"],
                    "stream_active": stream_active,
                }
                if "session_id" in payload:
                    ack["session_id"] = payload["session_id"]
                try:
                    status_socket.sendto(json.dumps(ack, separators=(",", ":")).encode("utf-8"), sender)
                except OSError:
                    pass
        except OSError as exc:
            logger_mp.warning("[App Stream] Teleop status receiver unavailable: %s", exc)
        finally:
            with self._lock:
                if self._status_socket is status_socket:
                    self._status_socket = None
            status_socket.close()

    def _subscribe_teleop_status(self, client: socket.socket, peer_host: str) -> None:
        """Forward accepted status to the App over a socket on the control IP.

        The App accepts status only from the camera host address it connected
        to, so the forwarding socket binds to the control connection's local
        IP rather than reusing the status receiver, which may listen on
        another address such as 127.0.0.1. If that bind fails, the App stays
        unsubscribed and receives no status.
        """
        forward_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            forward_socket.bind((client.getsockname()[0], 0))
        except OSError as exc:
            forward_socket.close()
            logger_mp.warning("[App Stream] Cannot forward teleop status to %s: %s", peer_host, exc)
            return
        self._replace_status_subscriber((peer_host, forward_socket))
        logger_mp.info("[App Stream] App-native teleop status subscribed from %s.", peer_host)

    def _replace_status_subscriber(self, subscriber: Optional[Tuple[str, socket.socket]]) -> None:
        """Set the App that receives forwarded status and close the old socket."""
        with self._lock:
            previous = self._status_subscriber
            self._status_subscriber = subscriber
        if previous is not None:
            try:
                previous[1].close()
            except OSError:
                pass

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
                self._replace_status_subscriber(None)
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
        """Process stream and status-subscription messages for one App connection.

        An empty destination host uses the control peer's IP. Disconnecting the
        active control client also clears its video request and status
        subscription.
        """
        try:
            while not self._stop_event.is_set():
                message = _receive_control_message(client, self._stop_event)
                if message is None:
                    break
                command, payload = message
                if command == OPEN_CAMERA:
                    self._accept_camera_request(parse_camera_stream_request(payload), peer_host)
                elif command == OPEN_STREAM:
                    if not self.stereo_enabled:
                        logger_mp.warning("[App Stream] OPEN_STREAM needs head camera roles; ignoring it.")
                        continue
                    self._accept_stream_request(parse_stream_request(payload), peer_host)
                elif command == REQUEST_KEYFRAME:
                    self._accept_keyframe_request(payload)
                elif command == CLOSE_CAMERA:
                    with self._lock:
                        self._set_request_locked(None, None)
                    logger_mp.info("[App Stream] Robotree App stopped the stream.")
                elif command == SUBSCRIBE_TELEOP_STATUS:
                    self._subscribe_teleop_status(client, peer_host)
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
                active = self._control_client is client
                if active:
                    self._control_client = None
                    self._set_request_locked(None, None)
            if active:
                self._replace_status_subscriber(None)
            try:
                client.close()
            except OSError:
                pass

    def _accept_camera_request(self, camera_request: CameraStreamRequest, peer_host: str) -> None:
        """Record a version-one request for the configured quad if it matches."""
        if camera_request.camera != self.stream_name:
            logger_mp.warning("[App Stream] Ignoring unsupported stream %r.", camera_request.camera)
            return
        width, height = camera_request.width, camera_request.height
        if self.video_config is not None:
            if (height, width) != self.quad_shape:
                logger_mp.warning(
                    "[App Stream] Requested dimensions %dx%d do not match configured %dx%d.",
                    width, height, self.quad_shape[1], self.quad_shape[0],
                )
                return
            fps, bitrate = self.fps, self.quad_bitrate
        else:
            fps, bitrate = camera_request.fps, camera_request.bitrate
        request = StreamRequest(
            version=1, request_id=0, camera=camera_request.camera,
            host=camera_request.host or peer_host, port=camera_request.port,
            render_mode="window", decoders=(("h264", width, height),),
            width=width, height=height,
        )
        geometry = select_stream_geometry(
            request, fps=fps, quad_shape=(height, width), quad_bitrate=bitrate,
            eye_shape=self.eye_shape, stereo_bitrate=self.stereo_bitrate,
            eye_source_shape=None,
        )
        with self._lock:
            self._set_request_locked(request, geometry)
        logger_mp.info(
            "[App Stream] Requested stream %s at %dx%d@%dfps to %s:%d.",
            request.camera, geometry.width, geometry.height, geometry.fps, request.host, request.port,
        )

    def _accept_stream_request(self, request: StreamRequest, peer_host: str) -> None:
        """Record a version-two request with the geometry chosen for it."""
        if request.camera != self.stream_name:
            logger_mp.warning("[App Stream] Ignoring unsupported stream %r.", request.camera)
            return
        if not request.host:
            request = StreamRequest(**{**request.__dict__, "host": peer_host})
        geometry = select_stream_geometry(
            request, fps=self.fps, quad_shape=self.quad_shape, quad_bitrate=self.quad_bitrate,
            eye_shape=self.eye_shape, stereo_bitrate=self.stereo_bitrate,
            eye_source_shape=self._eye_source_shape(), codecs=self.codecs,
        )
        if geometry is None:
            logger_mp.warning(
                "[App Stream] No common codec: App decodes %s, server encodes %s.",
                [codec for codec, _, _ in request.decoders], list(self.codecs),
            )
            return
        with self._lock:
            self._set_request_locked(request, geometry)
        logger_mp.info(
            "[App Stream] Requested %s stream #%d: %s %dx%d@%dfps up to %.1f Mbps to %s:%d.",
            request.render_mode, request.request_id, geometry.layout, geometry.width,
            geometry.height, geometry.fps, geometry.bitrate / 1e6, request.host, request.port,
        )

    def _accept_keyframe_request(self, payload: bytes) -> None:
        """Flag a keyframe for the matching version-two request, rate-limited."""
        try:
            request_id = json.loads(payload.decode("utf-8")).get("request_id")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            raise ValueError("REQUEST_KEYFRAME payload must be a JSON object.") from None
        now = time.monotonic()
        with self._lock:
            request = self._request
            if request is None or request.version != STREAM_PROTOCOL_VERSION or request.request_id != request_id:
                return
            if now - self._last_keyframe_request < _KEYFRAME_MIN_INTERVAL:
                return
            self._last_keyframe_request = now
            self._keyframe_requested = True

    def _set_request_locked(self, request: Optional[StreamRequest], geometry: Optional[StreamGeometry]) -> None:
        """Replace the request and start a new generation; lock required."""
        self._request = request
        self._geometry = geometry if request is not None else None
        self._generation += 1
        self._keyframe_requested = False

    def _step(self, now: float):
        """Follow the active request, connect if needed and flush pending bytes.

        Returns:
            (generation, request, geometry, keyframe flag), or None without a
            request.
        """
        with self._lock:
            generation = self._generation
            request = self._request
            geometry = self._geometry
            force_keyframe = self._keyframe_requested
        if generation != self._active_generation:
            self._reset_stream()
            self._active_generation = generation
        if request is None or geometry is None:
            return None
        if self._stream_socket is None and now >= self._next_connect_time:
            try:
                self._connect(request, geometry, now)
            except OSError as exc:
                self._next_connect_time = now + 1.0
                logger_mp.warning(
                    "[App Stream] Cannot connect to %s:%d: %s", request.host, request.port, exc,
                )
        if self._stream_socket is not None:
            try:
                self._flush(now)
            except (ConnectionError, OSError) as exc:
                logger_mp.warning("[App Stream] H264 stream interrupted: %s", exc)
                self._reset_stream()
                self._next_connect_time = now + 1.0
        return generation, request, geometry, force_keyframe

    def _connect(self, request: StreamRequest, geometry: StreamGeometry, now: float) -> None:
        """Connect the video socket and queue the RTSI message for version two."""
        sock = socket.create_connection((request.host, request.port), timeout=1.0)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Keep at most about one frame unsent in the kernel; leave the send
            # buffer to autotuning so throughput is not capped by its size.
            frame_bytes = max(16 * 1024, geometry.bitrate // 8 // max(1, geometry.fps))
            try:
                sock.setsockopt(socket.IPPROTO_TCP, _TCP_NOTSENT_LOWAT, frame_bytes)
            except OSError:
                pass
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._stream_socket = sock
        self._pending = bytearray()
        if request.version == STREAM_PROTOCOL_VERSION:
            info = geometry.stream_info(request.request_id)
            self._pending += len(info).to_bytes(4, "big") + info
        self._controller = BitrateController(
            maximum=geometry.bitrate, minimum=min(self.min_bitrate, geometry.bitrate), now=now,
        )
        self._stream_started_at = now
        self._last_progress = now
        self._stats_started_at = now
        logger_mp.info(
            "[App Stream] H264 stream connected to %s:%d (%s %dx%d).",
            request.host, request.port, geometry.layout, geometry.width, geometry.height,
        )

    def _flush(self, now: float) -> None:
        """Send queued bytes until the socket would block.

        Raises:
            ConnectionError: No byte was sent for longer than the stall timeout.
            OSError: The connection failed.
        """
        sock = self._stream_socket
        while self._pending:
            try:
                sent = sock.send(self._pending)
            except BlockingIOError:
                break
            if sent <= 0:
                break
            del self._pending[:sent]
            self._stats_bytes += sent
            self._last_progress = now
        if not self._pending:
            self._last_progress = now
        elif now - self._last_progress > _STALL_TIMEOUT:
            raise ConnectionError(f"no send progress for {_STALL_TIMEOUT:.0f} s")

    def _writable(self) -> bool:
        """Return whether the kernel accepts more data without blocking."""
        try:
            _, writable, _ = select.select([], [self._stream_socket], [], 0)
        except (OSError, ValueError):
            return False
        return bool(writable)

    def _next_pts(self, now: float) -> int:
        """Return a strictly increasing millisecond timestamp for the encoder."""
        pts = max(self._last_pts + 1, int((now - self._stream_started_at) * 1000))
        self._last_pts = pts
        return pts

    def _open_codec(self, geometry: StreamGeometry, bitrate: int):
        """Create the libx264 encoder for geometry with rate control.

        maxrate is the geometry's budget and the VBV buffer holds about one
        frame, so no frame bursts above the link budget; with intra refresh
        there are no periodic IDR frames. Options the encoder does not use
        raise instead of being ignored.

        Raises:
            ValueError: An encoder option was not consumed.
        """
        import av

        video = self.video_config or {}
        gop_length = int(video.get("gop_length", 2 * geometry.fps))
        vbv_seconds = float(video.get("vbv_seconds", DEFAULT_VBV_SECONDS))
        codec = av.CodecContext.create("libx264", "w")
        codec.width = geometry.width
        codec.height = geometry.height
        codec.pix_fmt = video.get("pixel_format", "yuv420p")
        codec.time_base = Fraction(1, 1000)
        codec.framerate = Fraction(geometry.fps, 1)
        codec.bit_rate = int(bitrate)
        options = {
            "preset": video.get("preset", DEFAULT_PRESET),
            "tune": video.get("tune", "zerolatency"),
            "threads": str(video.get("threads", DEFAULT_THREADS)),
            "g": str(gop_length),
            "keyint_min": str(gop_length),
            "sc_threshold": "0",
            "forced-idr": "1",
            "maxrate": str(geometry.bitrate),
            "bufsize": str(max(1, int(geometry.bitrate * vbv_seconds))),
        }
        if video.get("intra_refresh", True):
            options["intra-refresh"] = "1"
        codec.options = options
        codec.open()
        unused = dict(codec.options)
        if unused:
            raise ValueError(f"libx264 ignored encoder options: {sorted(unused)!r}.")
        return codec

    @staticmethod
    def _video_frame(bgr_frame: np.ndarray, geometry: StreamGeometry):
        """Validate BGR input and convert it to YUV420P at the geometry size."""
        import av

        if not isinstance(bgr_frame, np.ndarray):
            raise TypeError("Camera stream frame must be a numpy array.")
        if bgr_frame.ndim != 3 or bgr_frame.shape[2] != 3:
            raise ValueError("Camera stream frame must have shape (height, width, 3).")
        frame = av.VideoFrame.from_ndarray(
            np.ascontiguousarray(bgr_frame),
            format="bgr24",
        )
        return frame.reformat(width=geometry.width, height=geometry.height, format="yuv420p")

    def _reset_stream(self) -> None:
        """Close the video socket and encoder and drop unsent bytes."""
        sock = self._stream_socket
        self._stream_socket = None
        self._codec = None
        self._pending = bytearray()
        self._controller = None
        self._frames_encoded = 0
        self._last_pts = -1
        self._next_connect_time = 0.0
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _log_stats(self, now: float) -> None:
        """Log bitrate, unsent bytes, skips and encode time every few seconds."""
        if self._stream_socket is None or now - self._stats_started_at < _STATS_INTERVAL:
            return
        elapsed = now - self._stats_started_at
        encode_ms = sorted(self._stats_encode_ms)
        p95 = encode_ms[int(0.95 * (len(encode_ms) - 1))] if encode_ms else 0.0
        logger_mp.info(
            "[App Stream] target=%.1fMbps sent=%.1fMbps unsent=%dB encoded=%d skipped=%d encode_p95=%.1fms",
            (self._controller.target if self._controller else 0) / 1e6,
            self._stats_bytes * 8 / elapsed / 1e6,
            len(self._pending), self._stats_encoded, self._stats_skipped, p95,
        )
        self._stats_started_at = now
        self._stats_bytes = 0
        self._stats_encoded = 0
        self._stats_skipped = 0
        self._stats_encode_ms = []


def _intra_picture_type():
    """Return PyAV's intra picture type used to force an IDR frame."""
    import av

    return av.video.frame.PictureType.I


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
