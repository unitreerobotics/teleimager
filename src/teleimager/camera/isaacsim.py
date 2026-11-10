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

"""Cameras of the Isaac Sim / Isaac Lab simulator (unitree_sim_isaaclab), read from shared memory.

The simulator writes every camera image into its own POSIX shared memory block ``isaac_<name>_image_shm``
(``head`` / ``head_right`` = head stereo left / right eye, ``left`` / ``right`` = wrist cameras). Each block
holds a small header followed by the image (JPEG or raw BGR). The writer sets the timestamp to 0 while it
updates a block; a reader accepts a frame only if the timestamp is non-zero and unchanged across the copy.
"""

import ctypes
import os
import time
from multiprocessing import resource_tracker, shared_memory

import logging_mp
import numpy as np

from .camera import BaseCamera, _turbojpeg

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class SimImageHeader(ctypes.LittleEndianStructure):
    """Header of one simulator image block (unitree_sim_isaaclab tools/shared_memory_utils.py)."""

    _fields_ = [
        ("timestamp", ctypes.c_uint64),   # write time [ms]; 0 while the block is being written
        ("height", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("channels", ctypes.c_uint32),
        ("image_name", ctypes.c_char * 16),
        ("data_size", ctypes.c_uint32),
        ("encoding", ctypes.c_uint32),    # 0 = raw BGR, 1 = JPEG
        ("quality", ctypes.c_uint32),
    ]


_HEADER_SIZE = ctypes.sizeof(SimImageHeader)


def shm_name(image_name: str) -> str:
    """Shared memory name of a simulator image (prefix overridable with $TELEIMAGER_SIM_SHM_PREFIX)."""
    return f"{os.environ.get('TELEIMAGER_SIM_SHM_PREFIX', 'isaac_')}{image_name}_image_shm"


class SimImageReader:
    """Reads one simulator image block; returns only frames newer than the last one read."""

    def __init__(self, image_name: str):
        self._name = image_name
        self._shm = None
        self._last_ts = 0
        self._missing_logged = False

    def _open(self) -> bool:
        if self._shm is not None:
            return True
        try:
            self._shm = shared_memory.SharedMemory(name=shm_name(self._name))
            # The simulator owns the block: do not let this process's resource tracker unlink it on exit.
            try:
                resource_tracker.unregister(self._shm._name, "shared_memory")
            except Exception:
                pass
            return True
        except FileNotFoundError:
            if not self._missing_logged:
                logger_mp.info(f"[Teleimager] waiting for simulator image '{self._name}' ({shm_name(self._name)})")
                self._missing_logged = True
            return False

    def read_new(self):
        """Return (header, payload bytes) of a frame newer than the last one read, else None."""
        if not self._open():
            return None
        buf = self._shm.buf
        h1 = SimImageHeader.from_buffer_copy(bytes(buf[:_HEADER_SIZE]))
        if h1.timestamp == 0 or h1.timestamp == self._last_ts:
            return None  # being written, or nothing new
        end = _HEADER_SIZE + h1.data_size
        if h1.data_size == 0 or end > len(buf):
            return None
        payload = bytes(buf[_HEADER_SIZE:end])
        h2 = SimImageHeader.from_buffer_copy(bytes(buf[:_HEADER_SIZE]))
        if h2.timestamp != h1.timestamp or h2.data_size != h1.data_size:
            return None  # overwritten while copying: take the next one
        self._last_ts = h1.timestamp
        return h1, payload

    def close(self):
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None


def _to_bgr(header, payload):
    if header.encoding == 1:
        return _turbojpeg.decode(payload)
    return np.frombuffer(payload, dtype=np.uint8).reshape(header.height, header.width, header.channels)


class IsaacSimCamera(BaseCamera):
    """Camera of the simulator: one image block (monocular) or the head stereo pair side by side (binocular).

    Frames are published when the simulator writes a new one (no fixed-rate re-publishing). A monocular JPEG
    frame is passed to ZMQ as it is (no decode / re-encode).
    """

    # _update_frame() polls until a new frame arrives; no extra pacing needed
    _capture_requires_pacing = False
    _POLL_S = 0.002
    _MAX_WAIT_S = 0.1

    def __init__(self, cam_topic, server_cfg, image_sources=("head",)):
        """
        Args:
            cam_topic: camera topic name.
            server_cfg: camera settings (``binocular``, ``image_shape``, ports, ...).
            image_sources: simulator image names; two names (left, right eye) for a binocular camera.
        """
        super().__init__(cam_topic, server_cfg)
        self._binocular = len(image_sources) == 2
        self._sources = tuple(image_sources)
        self._readers = [SimImageReader(n) for n in self._sources]
        self._pending = [None] * len(self._readers)  # newest unpublished (header, payload) per source
        self._warned_single_eye = False
        # Ready once constructed; frames arrive when the simulator renders.
        self._ready.set()
        logger_mp.info(str(self))

    def __str__(self):
        zmq = f"zmq=on (port={self._zmq_port})" if self._enable_zmq else "zmq=off"
        webrtc = f"webrtc=on (port={self._webrtc_port})" if self._enable_webrtc else "webrtc=off"
        src = "+".join(self._sources)
        return (f" 📷 Camera {self._cam_topic!r:<22} isaacsim, {self._img_shape[0]}x{self._img_shape[1]}@{self._fps}fps, "
                f"source={src}, {'binocular' if self._binocular else 'monocular'}, {zmq}, {webrtc}")

    def _poll(self):
        """Collect new frames; True when every source has a new frame since the last publish."""
        for i, reader in enumerate(self._readers):
            frame = reader.read_new()
            if frame is not None:
                self._pending[i] = frame
        return all(p is not None for p in self._pending)

    def _update_frame(self):
        deadline = time.monotonic() + self._MAX_WAIT_S
        while not self._poll():
            if time.monotonic() >= deadline:
                if (self._binocular and not self._warned_single_eye
                        and self._pending[0] is not None and self._pending[1] is None):
                    self._warned_single_eye = True
                    logger_mp.warning(
                        f"[Teleimager] {self._cam_topic} is binocular but the simulator writes no '{self._sources[1]}' "
                        f"image: no frames are published. Does the task have a 'front_right_camera' (and is it in "
                        f"--camera_include)?")
                return
            time.sleep(self._POLL_S)
        frames, self._pending = self._pending, [None] * len(self._readers)

        if not self._binocular and frames[0][0].encoding == 1:
            # monocular JPEG: pass through
            _, jpeg = frames[0]
            if self._enable_zmq:
                self._zmq_buffer.write(jpeg)
            self._write_webrtc_jpeg(jpeg)
            if self._enable_bgr_output:
                self._bgr_buffer.write(_turbojpeg.decode(jpeg))
        else:
            images = [_to_bgr(h, p) for h, p in frames]
            bgr = images[0] if len(images) == 1 else np.ascontiguousarray(np.hstack(images))
            if self._enable_zmq:
                self._zmq_buffer.write(_turbojpeg.encode(bgr))
            self._write_webrtc_bgr(bgr)
            if self._enable_bgr_output:
                self._bgr_buffer.write(bgr)

    def release(self):
        for reader in self._readers:
            reader.close()
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @classmethod
    def scan(cls):
        """Virtual sources have no device discovery."""
        return []

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Select the simulator image(s) of a camera topic.

        ``image_source`` (optional) names the simulator image(s) explicitly: a name, or a [left, right] pair for
        a binocular camera. Otherwise:

        * binocular -> ("head", "head_right") (head stereo pair side by side)
        * head topics: "...right..." -> "head_right" (right eye), else "head" (left eye / mono head)
        * other topics: "...right..." -> "right", "...left..." -> "left" (wrist cameras), else "head"
        """
        src = server_cfg.get("image_source")
        topic = cam_topic.lower()
        if src:
            sources = (src,) if isinstance(src, str) else tuple(src)
        elif server_cfg.get("binocular", False):
            sources = ("head", "head_right")
        elif "head" in topic:
            sources = ("head_right",) if "right" in topic else ("head",)
        elif "right" in topic:
            sources = ("right",)
        elif "left" in topic:
            sources = ("left",)
        else:
            sources = ("head",)
        return cls(cam_topic, server_cfg, image_sources=sources)
