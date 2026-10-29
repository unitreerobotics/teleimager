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

"""Capture UVC MJPEG frames with libuvc and discover Linux camera identifiers."""

from pathlib import Path
import sys

import av
import logging_mp

from .camera import BaseCamera
from .realsense import RealSenseCamera

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class UVCCamera(BaseCamera):
    """Capture MJPEG with libuvc, preserving JPEG bytes and optional BGR output."""

    def __init__(self, cam_topic, uid, server_cfg):
        """Open the libuvc UID and select the exact configured MJPEG capture mode."""
        super().__init__(cam_topic, server_cfg)
        uvc = self.get_uvc_module()
        self.uid = uid
        self.cap = None
        try:
            self.cap = uvc.Capture(self.uid)
        except Exception as e:
            self.cap = None
            raise RuntimeError(
                f"[UVCCamera] Failed to open camera {self._cam_topic}: {e}\n"
                '    If this is a permissions problem:  sudo "$(which teleimager-server)" --setup-permissions')

        try:
            self.cap.frame_mode = self._choose_mode(self.cap, width=self._img_shape[1], height=self._img_shape[0], fps=self._fps)
            logger_mp.info(str(self))
        except Exception as e:
            self.cap = None
            raise RuntimeError(f"[UVCCamera] Failed to set mode for {self._cam_topic}: {e}")

    @staticmethod
    def get_uvc_module():
        """Load the uvc binding, exiting with installation guidance if unavailable."""
        try:
            import uvc
            return uvc
        except ImportError:
            msg = (
                "[UVC] pupil-labs-uvc (import name 'uvc') not installed.\n"
                f"  Interpreter: {sys.executable}\n"
                "  [1/2] System libraries pyuvc links against (before a source build):\n"
                "      sudo apt install -y libusb-1.0-0-dev libturbojpeg-dev\n"
                "  [2/2] The Python binding, from PyPI:\n"
                "      pip install \"teleimager[uvc]\"\n"
            )
            logger_mp.error(msg)
            sys.exit(1)

    def __str__(self):
        zmq = f"zmq=on (port={self._zmq_port})" if self._enable_zmq else "zmq=off"
        webrtc = f"webrtc=on (port={self._webrtc_port})" if self._enable_webrtc else "webrtc=off"
        return f" 📷 Camera {self._cam_topic!r:<22} uvc, {self._img_shape[0]}x{self._img_shape[1]}@{self._fps}fps, MJPG, {zmq}, {webrtc}"

    def _choose_mode(self, cap, width=None, height=None, fps=None):
        """Return the exact MJPEG resolution and FPS match, or raise ValueError."""
        for m in cap.available_modes:
            if m.width == width and m.height == height and m.fps == fps and m.format_name == "MJPG":
                return m
        raise ValueError("[UVCCamera] No matching uvc mode found")

    def _update_frame(self):
        """Capture one frame and refresh enabled JPEG and BGR buffers."""
        if self.cap is not None:
            frame = self.cap.get_frame_robust()
            if frame is not None:
                if self._enable_zmq:
                    if frame.jpeg_buffer is not None:
                        self._zmq_buffer.write(bytes(frame.jpeg_buffer))

                if self._enable_bgr_output:
                    if frame.bgr is not None:
                        self._bgr_buffer.write(frame.bgr)

                if not self._ready.is_set():
                    self._ready.set()
            else:
                raise RuntimeError

    def release(self):
        """Log release without calling libuvc stop or close.

        Explicit libuvc teardown is avoided because it can hang after a USB hub
        disconnects. This method does not explicitly close the capture handle.
        """
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @staticmethod
    def _list_video_paths():
        """List Linux video nodes exposed under the video4linux sysfs directory."""
        base = Path("/sys/class/video4linux")
        if not base.exists():
            return []
        return [f"/dev/{name}" for name in sorted(p.name for p in base.iterdir()) if name.startswith("video")]

    @staticmethod
    def _is_like_rgb(video_path):
        """Open a video node with PyAV and check whether its first frame is non-gray."""
        try:
            container = av.open(video_path, format="v4l2")
        except Exception:
            return False
        try:
            for frame in container.decode(container.streams.video[0]):
                return not frame.format.name.startswith("gray")
            return False
        except Exception:
            return False
        finally:
            container.close()

    @staticmethod
    def _get_ppath_from_vpath(video_path):
        """Resolve a video node to its sysfs device path."""
        sysfs_path = Path("/sys/class/video4linux") / Path(video_path).name / "device"
        return str(sysfs_path.resolve())

    @staticmethod
    def _get_uid_from_ppath(physical_path):
        """Read the USB bus and device numbers used as the libuvc UID."""
        def read_file(path):
            return path.read_text().strip() if path.exists() else None

        pdir = Path(physical_path)
        busnum_file = pdir / "busnum"
        devnum_file = pdir / "devnum"
        if not (busnum_file.exists() and devnum_file.exists()):
            pdir = pdir.parent
            busnum_file = pdir / "busnum"
            devnum_file = pdir / "devnum"
        if busnum_file.exists() and devnum_file.exists():
            return f"{read_file(busnum_file)}:{read_file(devnum_file)}"
        return None

    @staticmethod
    def _usb_attrs_from_ppath(physical_path):
        """Read USB identifiers and display attributes from the nearest USB parent.

        Uses sysfs without opening the camera. Missing attributes are returned as
        None; identifier fields match this backend's configuration keys.
        """
        def _read(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except Exception:
                return None

        d = Path(physical_path) if physical_path else None
        for _ in range(10):
            if d is None or not d.is_dir():
                break
            vid = _read(d / "idVendor")
            pid = _read(d / "idProduct")
            if vid and pid:
                return {
                    "serial_number": _read(d / "serial"),
                    "bcd_device": _read(d / "bcdDevice"),
                    "vid_pid": f"{vid}:{pid}",
                    "name": _read(d / "product"),
                    "manufacturer": _read(d / "manufacturer"),
                    "idVendor": vid,
                    "idProduct": pid,
                }
            if d.parent == d:
                break
            d = d.parent
        return {"serial_number": None, "bcd_device": None, "vid_pid": None,
                "name": None, "manufacturer": None, "idVendor": None, "idProduct": None}

    @classmethod
    def scan(cls):
        """Discover non-gray video sources and read their USB identifiers.

        Probes video nodes with PyAV, then reads physical_path, serial_number,
        bcd_device, vid_pid, video_id, and the libuvc UID from sysfs. RealSense
        exclusions are applied by CameraFinder and from_config(), not this scan.
        """
        cards = []
        for vpath in cls._list_video_paths():
            if not cls._is_like_rgb(vpath):
                continue
            ppath = cls._get_ppath_from_vpath(vpath)
            uid = cls._get_uid_from_ppath(ppath)
            attrs = cls._usb_attrs_from_ppath(ppath)
            cards.append({
                "type": "uvc",
                "video_path": vpath,
                "video_id": int(vpath.replace("/dev/video", "")),
                "physical_path": ppath,
                "uid": uid,
                "dev_info": {
                    "name": attrs["name"],
                    "manufacturer": attrs["manufacturer"],
                    "idVendor": attrs["idVendor"],
                    "idProduct": attrs["idProduct"],
                },
                "serial_number": attrs["serial_number"],
                "bcd_device": attrs["bcd_device"],
                "vid_pid": attrs["vid_pid"],
            })
        return cards

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Resolve one configured identifier and open the matching UVC camera.

        Priority is physical_path, serial_number, bcd_device, vid_pid, then
        video_id. A selected identifier does not fall back to the next one.
        Returns None if no match is found and raises ValueError for ambiguous
        matches requiring uniqueness. RealSense video nodes are excluded.
        """
        physical_path = str(server_cfg.get("physical_path")) if server_cfg.get("physical_path") else None
        serial_number = str(server_cfg.get("serial_number")) if server_cfg.get("serial_number") else None
        _bcd = server_cfg.get("bcd_device")
        bcd_device = f"{_bcd:04d}" if type(_bcd) is int else (str(_bcd) if _bcd else None)
        vid_pid = str(server_cfg.get("vid_pid")) if server_cfg.get("vid_pid") else None

        # Apply RealSense exclusions here as well as in the discovery report.
        rs_paths = set(RealSenseCamera._list_video_paths())
        cards = [c for c in cls.scan() if c["video_path"] not in rs_paths]

        def _uid(key, value, unique):
            matches = [c for c in cards if c.get(key) == value]
            if unique and len(matches) > 1:
                raise ValueError(f"Multiple UVCCamera found with {key} {value}")
            return matches[0]["uid"] if matches else None

        if physical_path is not None:
            uid = _uid("physical_path", physical_path, unique=False)
            if uid is None:
                logger_mp.error(f"[Teleimager] Cannot find UVCCamera for {cam_topic} with physical path {physical_path}")
                return None
            else:
                return cls(cam_topic, uid, server_cfg)

        if serial_number is not None:
            uid = _uid("serial_number", serial_number, unique=True)
            if uid is None:
                logger_mp.error(f"[Teleimager] Cannot find UVCCamera for {cam_topic} with serial number {serial_number}")
                return None
            return cls(cam_topic, uid, server_cfg)

        if bcd_device is not None:
            uid = _uid("bcd_device", bcd_device, unique=True)
            if uid is None:
                logger_mp.error(f"[Teleimager] Cannot find UVCCamera for {cam_topic} with bcd_device {bcd_device}")
                return None
            return cls(cam_topic, uid, server_cfg)

        if vid_pid is not None:
            uid = _uid("vid_pid", vid_pid, unique=True)
            if uid is None:
                logger_mp.error(f"[Teleimager] Cannot find UVCCamera for {cam_topic} with vid_pid {vid_pid}")
                return None
            return cls(cam_topic, uid, server_cfg)

        video_id = server_cfg.get("video_id", 0)
        video_id = int(video_id) if video_id is not None else 0
        uid = _uid("video_id", video_id, unique=True)
        if uid is None:
            logger_mp.error(f"[Teleimager] Cannot find UVCCamera for {cam_topic} with video_id {video_id}")
            return None
        return cls(cam_topic, uid, server_cfg)
