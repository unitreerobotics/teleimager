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

"""Capture RealSense color images and optional depth aligned to color."""

from pathlib import Path
import glob
import platform
import re
import subprocess
import sys

import logging_mp
import numpy as np

from .camera import BaseCamera, _turbojpeg

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class RealSenseCamera(BaseCamera):
    """Read color and optional aligned depth frames through the RealSense SDK."""

    def __init__(self, cam_topic, serial_number, server_cfg):
        """Open the selected serial number with BGR color and optional z16 depth."""
        rs = self.get_realsense_module()
        super().__init__(cam_topic, server_cfg)
        self._serial_number = serial_number
        self._enable_depth = server_cfg.get("enable_depth", False)
        self._latest_depth = None
        try:
            align_to = rs.stream.color
            self.align = rs.align(align_to)
            self.pipeline = rs.pipeline()
            config = rs.config()
            config.enable_device(self._serial_number)

            config.enable_stream(rs.stream.color, self._img_shape[1], self._img_shape[0], rs.format.bgr8, self._fps)
            if self._enable_depth:
                config.enable_stream(rs.stream.depth, self._img_shape[1], self._img_shape[0], rs.format.z16, self._fps)

            profile = self.pipeline.start(config)
            self._device = profile.get_device()
            if self._device is None:
                logger_mp.error('[Teleimager] pipe_profile.get_device() is None .')
            if self._enable_depth:
                assert self._device is not None
                depth_sensor = self._device.first_depth_sensor()
                self.g_depth_scale = depth_sensor.get_depth_scale()

            self.intrinsics = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
            logger_mp.info(str(self))
        except Exception as e:
            if getattr(self, "pipeline", None):
                try:
                    self.pipeline.stop()
                except:
                    pass
            raise RuntimeError(f"[RealSenseCamera] Failed to initialize RealSense camera {self._serial_number}: {e}")

    def __str__(self):
        zmq = f"zmq=on (port={self._zmq_port})" if self._enable_zmq else "zmq=off"
        webrtc = f"webrtc=on (port={self._webrtc_port})" if self._enable_webrtc else "webrtc=off"
        return f" 📷 Camera {self._cam_topic!r:<22} realsense, {self._img_shape[0]}x{self._img_shape[1]}@{self._fps}fps, {zmq}, {webrtc}"

    def _update_frame(self):
        """Wait for aligned frames and refresh color and optional depth buffers."""
        frames = self.pipeline.wait_for_frames()
        aligned_frames = self.align.process(frames)
        color_frame = aligned_frames.get_color_frame()
        if not color_frame:
            return None

        if self._enable_depth:
            depth_frame = aligned_frames.get_depth_frame()
            if depth_frame:
                self._latest_depth = np.asanyarray(depth_frame.get_data())
            else:
                self._latest_depth = None

        bgr_numpy = np.asanyarray(color_frame.get_data())

        if self._enable_bgr_output:
            self._bgr_buffer.write(bgr_numpy)

        if self._enable_zmq:
            self._zmq_buffer.write(_turbojpeg.encode(bgr_numpy))

        if not self._ready.is_set():
            self._ready.set()

    def get_depth_frame(self):
        """Return the latest aligned z16 depth bytes, or None if unavailable.

        Capture updates this data in _update_frame(). The image shape matches the
        color stream; multiply raw depth values by g_depth_scale to obtain meters.
        """
        if self._latest_depth is None:
            return None
        return self._latest_depth.tobytes()

    def release(self):
        """Stop the SDK pipeline and clear its reference."""
        if self.pipeline is not None and hasattr(self.pipeline, "stop"):
            try:
                self.pipeline.stop()
            except Exception as e:
                logger_mp.warning(f"[Teleimager] pipeline.stop() failed: {e}")
        self.pipeline = None
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @staticmethod
    def get_realsense_module():
        """Load pyrealsense2, exiting with installation guidance if unavailable."""
        try:
            import pyrealsense2 as rs
            return rs
        except ImportError:
            arch = platform.machine()
            system = platform.system()
            print(f"[RealSense] Platform: {system} / {arch}")

            msg = (
                "[RealSense] pyrealsense2 not installed. Try pip first:\n"
                "    pip install pyrealsense2\n"
            )

            if system == "Linux" and arch.startswith("aarch64"):
                msg += (
                    "  On ARM/Jetson a matching wheel isn't guaranteed for every Python version.\n"
                    "  If pip reports no matching wheel, build from source instead:\n"
                    "    git clone https://github.com/IntelRealSense/librealsense.git\n"
                    "    cd librealsense && git checkout v2.50.0   # or a tag matching your firmware\n"
                    "    mkdir build && cd build\n"
                    "    cmake .. -DBUILD_PYTHON_BINDINGS=ON -DPYTHON_EXECUTABLE=$(which python3)\n"
                    "    make -j$(nproc) && sudo make install\n"
                )
            logger_mp.error(msg)
            sys.exit(1)

    @classmethod
    def _list_serial_numbers(cls):
        """List SDK-visible RealSense device serial numbers."""
        rs = cls.get_realsense_module()
        ctx = rs.context()
        serials = []
        for dev in ctx.query_devices():
            try:
                serials.append(dev.get_info(rs.camera_info.serial_number))
            except Exception:
                continue
        return serials

    @staticmethod
    def _list_video_paths():
        """Find Linux video nodes with a RealSense name and Intel USB vendor ID."""
        def _read_text(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except Exception:
                return None

        def _parent_usb_device_sysdir(video_sysdir):
            d = (Path(video_sysdir) / "device").resolve()
            for _ in range(10):
                if not d.is_dir():
                    break
                id_vendor = _read_text(d / "idVendor")
                id_product = _read_text(d / "idProduct")
                if id_vendor and id_product:
                    return str(d)
                if d.parent == d:
                    break
                d = d.parent
            return None

        ports = []
        for devnode in sorted(glob.glob("/dev/video*")):
            sysdir = Path("/sys/class/video4linux") / Path(devnode).name
            name = _read_text(sysdir / "name")
            usb_dir = _parent_usb_device_sysdir(sysdir)
            vendor_id = _read_text(Path(usb_dir) / "idVendor") if usb_dir else None

            if name and "realsense" in name.lower() and (vendor_id or "").lower() in ("8086", "32902"):
                ports.append(devnode)

        return ports

    # Separate depth/infrared nodes from color sources in discovery reports.
    # V4L2 and GStreamer may report color nodes; UVC excludes all RealSense nodes.
    _DEPTH_IR_FMTS = {"Z16", "GREY", "Y8", "Y8I", "Y12I", "Y16", "Y10", "RW16", "W10", "CONFIDENCE"}

    @staticmethod
    def _list_pixel_formats(video_path):
        """Return advertised FourCC formats, or an empty set if querying fails."""
        try:
            r = subprocess.run(["v4l2-ctl", "-d", video_path, "--list-formats"],
                               capture_output=True, text=True, timeout=5)
            out = r.stdout or ""
        except Exception:
            return set()
        return {m.group(1) for m in re.finditer(r"\[\d+\]:\s*'(\w+)", out)}

    @classmethod
    def _classify_video_nodes(cls):
        """Split RealSense video nodes into color and depth/infrared lists.

        Classify by advertised capture formats and ignore metadata nodes that
        expose none. The discovery report uses this to filter other backends.
        """
        color, depth_ir = [], []
        for vpath in cls._list_video_paths():
            fmts = cls._list_pixel_formats(vpath)
            if not fmts:
                continue
            if fmts & cls._DEPTH_IR_FMTS:
                depth_ir.append(vpath)
            else:
                color.append(vpath)
        return color, depth_ir

    @classmethod
    def _list_depth_ir_paths(cls):
        """Return depth and infrared nodes excluded from V4L2 and GStreamer reports."""
        return cls._classify_video_nodes()[1]

    @classmethod
    def scan(cls):
        """Return RealSense serial numbers, video paths, and color capture modes."""
        serials = cls._list_serial_numbers()
        video_paths = cls._list_video_paths()
        cards = []
        for sn in serials:
            cards.append({"type": "realsense", "serial_number": sn,
                          "video_paths": video_paths, "modes": cls._list_modes(sn)})
        if not serials and video_paths:
            cards.append({"type": "realsense", "serial_number": None,
                          "video_paths": video_paths, "modes": []})
        return cards

    @classmethod
    def _list_modes(cls, serial_number):
        """List SDK color modes grouped by format and resolution.

        Each mapping contains stream, format, width, height, and sorted fps values.
        Depth, infrared, and motion stream profiles are not included.
        """
        rs = cls.get_realsense_module()
        ctx = rs.context()
        dev = None
        for d in ctx.query_devices():
            try:
                if d.get_info(rs.camera_info.serial_number) == serial_number:
                    dev = d
                    break
            except Exception:
                continue
        if dev is None:
            return []
        grouped = {}  # (stream, format, w, h) -> set(fps)
        for sensor in dev.query_sensors():
            for p in sensor.get_stream_profiles():
                try:
                    if p.stream_name() != "Color":
                        continue
                    vp = p.as_video_stream_profile()
                    key = (p.stream_name(), str(p.format()).split(".")[-1],
                           vp.width(), vp.height())
                    grouped.setdefault(key, set()).add(p.fps())
                except Exception:
                    continue
        modes = []
        for (stream, fmt, w, h), fps_set in grouped.items():
            modes.append({"stream": stream, "format": fmt, "width": w,
                          "height": h, "fps": sorted(fps_set)})
        return modes

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Open the configured serial number, or return None if it is absent."""
        serial_number = str(server_cfg.get("serial_number")) if server_cfg.get("serial_number") else None
        serials = [c.get("serial_number") for c in cls.scan()]
        if serial_number not in serials:
            logger_mp.error(f"[Teleimager] Cannot find RealSenseCamera for {cam_topic}")
            return None
        return cls(cam_topic, serial_number, server_cfg)
