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


"""Store the latest frame references and expose client image and depth data."""

from typing import Any, Optional
import struct
import threading

import cv2
import logging_mp
import numpy as np

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


# zstandard is optional: only the depth stream needs it.
try:
    import zstandard as zstd
except ImportError:
    zstd = None


def fit_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Return frame fitted into width x height with aspect ratio kept.

    A frame within about 2% of the target in both dimensions is center-cropped
    or padded instead of resampled, so near-native sources lose no sharpness.
    Downscaling uses INTER_AREA; the rest of the canvas is black.
    """
    source_height, source_width = frame.shape[:2]
    if (source_width, source_height) == (width, height):
        return frame
    output = np.zeros((height, width, 3), dtype=np.uint8)
    if abs(source_width - width) <= 0.02 * width and abs(source_height - height) <= 0.02 * height:
        crop_x, crop_y = max(0, (source_width - width) // 2), max(0, (source_height - height) // 2)
        cropped = frame[crop_y:crop_y + height, crop_x:crop_x + width]
        x, y = (width - cropped.shape[1]) // 2, (height - cropped.shape[0]) // 2
        output[y:y + cropped.shape[0], x:x + cropped.shape[1]] = cropped
        return output
    scale = min(width / source_width, height / source_height)
    resized_width = max(1, round(source_width * scale))
    resized_height = max(1, round(source_height * scale))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    x, y = (width - resized_width) // 2, (height - resized_height) // 2
    output[y:y + resized_height, x:x + resized_width] = cv2.resize(
        frame, (resized_width, resized_height), interpolation=interpolation,
    )
    return output


class TripleRingBuffer:
    """Share the latest value between a producer and a consumer.

    Writes skip the last slot selected by the reader. Values are stored and
    returned by reference; this buffer does not copy arrays or queue history.
    """

    def __init__(self):
        """Initialize three empty slots, their shared lock and the write sequence."""
        self.buffer = [None, None, None]
        self.write_index = 0            # Index where the next write will occur
        self.latest_index = -1          # Index of the latest written data
        self.read_index = -1            # Index of the current read data
        self.lock = threading.Lock()
        self._cond = threading.Condition(self.lock)
        self._seq = 0                   # Incremented on every write()

    def write(self, data):
        """Replace the latest value while avoiding the current read slot.

        Wakes every reader blocked in wait_new().
        """
        with self._cond:
            self.buffer[self.write_index] = data
            self.latest_index = self.write_index
            self.write_index = (self.write_index + 1) % 3
            if self.write_index == self.read_index:
                self.write_index = (self.write_index + 1) % 3
            self._seq += 1
            self._cond.notify_all()

    def read(self):
        """Return the latest value by reference, or None before the first write."""
        with self.lock:
            if self.latest_index == -1:
                return None  # No data has been written yet
            self.read_index = self.latest_index
        return self.buffer[self.read_index]

    def wait_new(self, last_seq, timeout=None):
        """Wait until a write newer than last_seq arrives.

        Args:
            last_seq: Write sequence the caller has already consumed; 0 before
                the first write.
            timeout: Longest wait in seconds, or None to wait indefinitely.

        Returns:
            The current write sequence, equal to last_seq on timeout.
        """
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq, timeout)
            return self._seq


class DepthPacket:
    """Carry one depth frame on the wire: a small header and a zstd payload.

    The header is struct "<IHHf": color frame number, width, height and
    depth_scale; the payload is the zstd-compressed row-major uint16 depth
    image. Depth is aligned to the color frame by the RealSense SDK; frames
    whose alignment failed are never sent.

    Attributes:
        frame_no: Number of the color frame this depth is aligned to.
        depth: uint16 depth image of shape (height, width).
        depth_scale: Meters per depth unit.
        compressed: The received zstd frame (the bytes after the header) for
            decoded packets, so a consumer can store it without recompressing;
            None for packets built locally.
    """

    available = zstd is not None and hasattr(zstd, "compress") and hasattr(zstd, "decompress")
    _HEADER = struct.Struct("<IHHf")  # frame no, width, height, depth_scale
    _LEVEL = 3  # zstd default level; compression is lossless, so it only trades speed for size

    def __init__(self, frame_no: int, depth: np.ndarray, depth_scale: float, compressed: Optional[bytes] = None):
        """Store one depth frame without copying it.

        Args:
            frame_no: Number of the color frame this depth is aligned to.
            depth: uint16 depth image of shape (height, width).
            depth_scale: Meters per depth unit.
            compressed: Received zstd payload, or None for a local packet.
        """
        self.frame_no = frame_no
        self.depth = depth
        self.depth_scale = depth_scale
        self.compressed = compressed

    def encode(self) -> bytes:
        """Return the header followed by the zstd-compressed depth image.

        Raises:
            RuntimeError: zstandard is not installed.
        """
        if not self.available:
            raise RuntimeError("[Teleimager] depth streaming requires zstandard: pip install 'teleimager[depth]'")
        header = self._HEADER.pack(self.frame_no & 0xFFFFFFFF, self.depth.shape[1], self.depth.shape[0], self.depth_scale)
        return header + zstd.compress(np.ascontiguousarray(self.depth).tobytes(), self._LEVEL)

    @classmethod
    def decode(cls, packet: bytes) -> "DepthPacket":
        """Parse one received message and keep its zstd payload as compressed.

        Raises:
            RuntimeError: zstandard is not installed.
            struct.error, ValueError: The message is malformed.
        """
        if not cls.available:
            raise RuntimeError("[Teleimager] decoding depth requires zstandard: pip install 'teleimager[depth]'")
        frame_no, width, height, scale = cls._HEADER.unpack_from(packet)
        compressed = packet[cls._HEADER.size:]
        depth = np.frombuffer(zstd.decompress(compressed), np.uint16).reshape(height, width)
        return cls(frame_no, depth, scale, compressed=compressed)


class TeleImage:
    """Expose JPEG bytes, optional decoded BGR data, depth, and measured receive FPS.

    BGR decoding may be disabled, pending, or available. JPEG, BGR, and depth
    values come from separate buffers and need not represent the same source
    frame; a DepthPacket carries the number of the color frame it belongs to.
    """

    _NOT_SET = object()
    __slots__ = ['jpg', '_bgr', 'fps', 'depth']

    def __init__(self, fps: float, jpg: Optional[bytes], bgr: Any = _NOT_SET,
                 depth: Optional[DepthPacket] = None):
        """Store frame data without copying it.

        Args:
            fps: Measured receive rate, in frames per second.
            jpg: Latest JPEG bytes, or None when unavailable.
            bgr: Decoded BGR array, None when unavailable, or the default sentinel
                when decoding is disabled.
            depth: Latest depth packet, or None when unavailable or not requested.
        """
        self.fps = fps
        self.jpg = jpg
        self._bgr = bgr
        self.depth = depth

    @property
    def bgr(self) -> Optional[np.ndarray]:
        """Return the decoded BGR array, or None if unavailable or disabled."""
        if self._bgr is TeleImage._NOT_SET:
            logger_mp.warning(f"[Teleimager] Accessing .bgr but decoding was DISABLED.")
            return None
        if self._bgr is None:
            logger_mp.debug(f"[Teleimager] Accessing .bgr but no image data received.")
            return None
        return self._bgr

    def __bool__(self):
        """Return whether nonempty JPEG bytes are available."""
        return bool(self.jpg)

    def __iter__(self):
        """Yield (fps, jpg, bgr), using None when BGR decoding is disabled."""
        yield self.fps
        yield self.jpg
        yield (None if self._bgr is TeleImage._NOT_SET else self._bgr)

    def __repr__(self):
        """Return the receive rate, JPEG size, BGR state, and depth frame for debugging."""
        size = len(self.jpg) if self.jpg else 0
        depth_frame = None if self.depth is None else self.depth.frame_no
        state = "DISABLED" if self._bgr is TeleImage._NOT_SET else ("FAILED" if self._bgr is None else "OK")
        return f"TeleImage(fps={self.fps:.1f}, jpg_byte_size={size}, bgr_state={state}, depth_frame_no={depth_frame})"
