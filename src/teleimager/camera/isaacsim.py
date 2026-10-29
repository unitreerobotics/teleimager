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

"""Read simulator images through the Isaac Lab shared-memory image reader."""

import logging_mp
import numpy as np

from .camera import BaseCamera, _turbojpeg

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class IsaacSimCamera(BaseCamera):
    """Read shared-memory images and expose JPEG and BGR output buffers.

    Binocular mode concatenates the left and right images horizontally.
    Reads do not block for capture, so the server paces them at the configured FPS.
    """

    _capture_requires_pacing = True

    def __init__(self, cam_topic, server_cfg, image_source="head"):
        """Attach the shared-memory reader and mark the backend initialized.

        Args:
            cam_topic: Camera topic used to identify the source.
            server_cfg: Native camera settings, including the binocular flag.
            image_source: Shared-memory source name for monocular reads.
        """
        super().__init__(cam_topic, server_cfg)
        from tools.shared_memory_utils import MultiImageReader # https://github.com/unitreerobotics/unitree_sim_isaaclab/tree/main/tools
        self.multi_image_reader = MultiImageReader()
        self._image_source = image_source
        self._binocular = server_cfg.get("binocular", False)
        # Readiness marks reader initialization, not the arrival of an image.
        self._ready.set()
        logger_mp.info(str(self))

    def __str__(self):
        zmq = f"zmq=on (port={self._zmq_port})" if self._enable_zmq else "zmq=off"
        webrtc = f"webrtc=on (port={self._webrtc_port})" if self._enable_webrtc else "webrtc=off"
        extra = f"source={self._image_source!r}, {'binocular' if self._binocular else 'monocular'}"
        return f" 📷 Camera {self._cam_topic!r:<22} isaacsim, {self._img_shape[0]}x{self._img_shape[1]}@{self._fps}fps, {extra}, {zmq}, {webrtc}"

    def _update_frame(self):
        """Read available source images and refresh enabled buffers without waiting."""
        frame_data = None
        if self._binocular:
            left_img = self.multi_image_reader.read_single_image('left')
            right_img = self.multi_image_reader.read_single_image('right')
            logger_mp.debug(f"[Teleimager] {self._cam_topic} - left: {left_img is not None}, right: {right_img is not None}")

            if left_img is not None and right_img is not None:
                frame_data = np.hstack([left_img, right_img])
                logger_mp.debug(f"[Teleimager] {self._cam_topic} - concatenated binocular frame: {frame_data.shape}")
        else:
            frame_data = self.multi_image_reader.read_single_image(self._image_source)
            if frame_data is None:
                logger_mp.debug(f"[Teleimager] {self._cam_topic} - no data for source '{self._image_source}'")

        if frame_data is not None:
            if self._enable_zmq:
                self._zmq_buffer.write(_turbojpeg.encode(frame_data))

            if self._enable_bgr_output:
                self._bgr_buffer.write(frame_data)

            if not self._ready.is_set():
                self._ready.set()
        else:
            logger_mp.debug(f"[Teleimager] No data available for {self._cam_topic}, frame_data is None")

    def release(self):
        """Close the shared-memory reader and clear its reference."""
        if hasattr(self, 'multi_image_reader') and self.multi_image_reader is not None:
            self.multi_image_reader.close()
        self.multi_image_reader = None
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @classmethod
    def scan(cls):
        """Return an empty list because virtual sources have no device discovery."""
        return []

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Select the shared-memory source from the topic and binocular setting."""
        binocular = server_cfg.get("binocular", False)

        if binocular:
            # Binocular reads always select both eyes, ignoring image_source.
            image_source = "head"
        else:
            if "left" in cam_topic.lower():
                image_source = "left"
            elif "right" in cam_topic.lower():
                image_source = "right"
            else:
                image_source = "head"

        return cls(cam_topic, server_cfg, image_source=image_source)
