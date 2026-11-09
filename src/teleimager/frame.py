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


"""Store the latest frame references and expose client image data."""

from typing import Any, Optional
import threading

import cv2
import logging_mp
import numpy as np

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


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
        """Initialize three empty slots and their shared index lock."""
        self.buffer = [None, None, None]
        self.write_index = 0            # Index where the next write will occur
        self.latest_index = -1          # Index of the latest written data
        self.read_index = -1            # Index of the current read data
        self.lock = threading.Lock()

    def write(self, data):
        """Replace the latest value while avoiding the current read slot."""
        with self.lock:
            self.buffer[self.write_index] = data
            self.latest_index = self.write_index
            self.write_index = (self.write_index + 1) % 3
            if self.write_index == self.read_index:
                self.write_index = (self.write_index + 1) % 3

    def read(self):
        """Return the latest value by reference, or None before the first write."""
        with self.lock:
            if self.latest_index == -1:
                return None  # No data has been written yet
            self.read_index = self.latest_index
        return self.buffer[self.read_index]


class TeleImage:
    """Expose JPEG bytes, optional decoded BGR data, and measured receive FPS.

    BGR decoding may be disabled, pending, or available. JPEG and BGR values
    come from separate buffers and need not represent the same source frame.
    """

    _NOT_SET = object()
    __slots__ = ['jpg', '_bgr', 'fps']

    def __init__(self, fps: float, jpg: Optional[bytes], bgr: Any = _NOT_SET):
        """Store frame data without copying it.

        Args:
            fps: Measured receive rate, in frames per second.
            jpg: Latest JPEG bytes, or None when unavailable.
            bgr: Decoded BGR array, None when unavailable, or the default sentinel
                when decoding is disabled.
        """
        self.fps = fps
        self.jpg = jpg
        self._bgr = bgr

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
        """Return the receive rate, JPEG size, and BGR state for debugging."""
        size = len(self.jpg) if self.jpg else 0
        state = "DISABLED" if self._bgr is TeleImage._NOT_SET else ("FAILED" if self._bgr is None else "OK")
        return f"TeleImage(fps={self.fps:.1f}, jpg_byte_size={size}, bgr_state={state})"
