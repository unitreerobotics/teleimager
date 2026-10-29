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

"""Capture Linux V4L2 video through PyAV and resolve native device identifiers."""

from pathlib import Path
import re
import shutil
import subprocess

import av
import logging_mp

from .camera import BaseCamera, _turbojpeg

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class V4L2Camera(BaseCamera):
    """Capture MJPEG directly or convert YUYV frames into configured outputs."""

    def __init__(self, cam_topic, video_path, server_cfg):
        """Open and test a video node, preferring MJPEG when advertised."""
        super().__init__(cam_topic, server_cfg)
        self._video_path = video_path

        # Prefer MJPEG passthrough; use YUYV when MJPEG is not advertised.
        if not shutil.which("v4l2-ctl"):
            raise RuntimeError(
                f"[V4L2Camera] Camera {self._cam_topic}: v4l2-ctl is required to pick the "
                "pixel format. Install it:  sudo apt install -y v4l-utils")
        info = self._v4l2_device_info(video_path) or {}
        formats = {m["format"] for m in info.get("modes", [])}
        self._passthrough = "MJPG" in formats
        input_format = "mjpeg" if self._passthrough else "yuyv422"

        self.container = av.open(self._video_path, format="v4l2", options={
            "input_format": input_format,
            "video_size": f"{self._img_shape[1]}x{self._img_shape[0]}",
            "framerate": str(self._fps),
        })
        self._demux = self.container.demux(self.container.streams.video[0])

        try:
            can_read = self._can_read_frame()
        except Exception:
            self.release()
            raise
        if not can_read:
            self.release()
            raise RuntimeError(f"[V4L2Camera] Camera {self._cam_topic} failed to initialize or read frames.")
        else:
            logger_mp.info(str(self))

    def __str__(self):
        zmq = f"zmq=on (port={self._zmq_port})" if self._enable_zmq else "zmq=off"
        webrtc = f"webrtc=on (port={self._webrtc_port})" if self._enable_webrtc else "webrtc=off"
        return f" 📷 Camera {self._cam_topic!r:<22} v4l2, {self._img_shape[0]}x{self._img_shape[1]}@{self._fps}fps, {zmq}, {webrtc}"

    def _can_read_frame(self):
        """Consume one packet and return whether it contains frame data."""
        try:
            return len(bytes(next(self._demux))) > 0
        except StopIteration:
            return False

    def _update_frame(self):
        """Capture a packet and refresh enabled JPEG and BGR output buffers."""
        if self.container is None:
            return
        packet = next(self._demux)
        if self._passthrough:
            # Preserve JPEG bytes for ZMQ; decode only for requested BGR output.
            jpeg_bytes = bytes(packet)
            if not jpeg_bytes:
                return
            if self._enable_zmq:
                self._zmq_buffer.write(jpeg_bytes)
            if self._enable_bgr_output:
                self._bgr_buffer.write(_turbojpeg.decode(jpeg_bytes))
        else:
            # YUYV requires conversion to BGR and JPEG encoding for ZMQ.
            frames = packet.decode()
            if not frames:
                return
            bgr_numpy = frames[0].to_ndarray(format="bgr24")
            if self._enable_bgr_output:
                self._bgr_buffer.write(bgr_numpy)
            if self._enable_zmq:
                self._zmq_buffer.write(_turbojpeg.encode(bgr_numpy))

        if not self._ready.is_set():
            self._ready.set()

    def release(self):
        """Close the PyAV capture container and clear its reference."""
        c = getattr(self, "container", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        self.container = None
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @staticmethod
    def _v4l2_device_info(video_path):
        """Query capture capabilities and modes with v4l2-ctl.

        Returns None when the capability query produces no output, including when
        the command is unavailable or fails to run.
        """
        def _run(extra):
            try:
                r = subprocess.run(["v4l2-ctl", "-d", video_path] + extra,
                                   capture_output=True, text=True, timeout=5)
                return r.stdout or ""
            except Exception:
                return ""

        txt = _run(["--all"])
        if not txt:
            return None

        modes = []
        cur_fmt = None
        for line in _run(["--list-formats-ext"]).splitlines():
            mfmt = re.search(r"\[\d+\]:\s*'(\w+)'", line)
            msize = re.search(r"Size:\s*\w+\s+(\d+)x(\d+)", line)
            mint = re.search(r"Interval:.*\(([\d.]+)\s*fps\)", line)
            if mfmt:
                cur_fmt = mfmt.group(1)
            elif msize:
                modes.append({"format": cur_fmt, "width": int(msize.group(1)),
                              "height": int(msize.group(2)), "fps": []})
            elif mint and modes:
                modes[-1]["fps"].append(float(mint.group(1)))

        return {
            "is_capture": "Video Capture" in txt,
            "modes": modes,
        }

    @staticmethod
    def _can_grab(video_path):
        """Open a video node with PyAV and test for a nonempty packet."""
        try:
            container = av.open(video_path, format="v4l2")
        except Exception:
            return False
        try:
            for packet in container.demux(container.streams.video[0]):
                return len(bytes(packet)) > 0
            return False
        except Exception:
            return False
        finally:
            container.close()

    # Read identifiers independently of UVC so devices inaccessible to libuvc
    # can still be selected through the V4L2 backend.

    @staticmethod
    def _get_ppath_from_vpath(video_path):
        """Resolve a video node to its sysfs device path."""
        sysfs_path = Path("/sys/class/video4linux") / Path(video_path).name / "device"
        return str(sysfs_path.resolve())

    @staticmethod
    def _usb_attrs_from_ppath(physical_path):
        """Read USB identifiers and display attributes from the nearest USB parent.

        Missing attributes are None, including for non-USB sources. vid_pid uses
        the same hexadecimal vendor:product representation as the UVC backend.
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
                }
            if d.parent == d:
                break
            d = d.parent
        return {"serial_number": None, "bcd_device": None, "vid_pid": None,
                "name": None, "manufacturer": None}

    @classmethod
    def _list_identifiers(cls):
        """Read video-node identifiers from sysfs without opening capture devices."""
        base = Path("/sys/class/video4linux")
        if not base.exists():
            return []
        out = []
        for name in sorted(p.name for p in base.iterdir()):
            if not name.startswith("video"):
                continue
            vpath = f"/dev/{name}"
            ppath = cls._get_ppath_from_vpath(vpath)
            attrs = cls._usb_attrs_from_ppath(ppath)
            out.append({
                "video_path": vpath,
                "video_id": int(name.replace("video", "")),
                "physical_path": ppath,
                "serial_number": attrs["serial_number"],
                "bcd_device": attrs["bcd_device"],
                "vid_pid": attrs["vid_pid"],
                "name": attrs["name"],
                "manufacturer": attrs["manufacturer"],
            })
        return out

    @classmethod
    def _resolve_vpath(cls, ids, key, value):
        """Resolve an identifier, preferring nodes that yield capture data.

        Falls back to the original matches if no node yields a packet. Returns
        None for no match and raises ValueError if multiple candidates remain.
        """
        matches = [r["video_path"] for r in ids if r.get(key) == value]
        if not matches:
            return None
        grabbable = [v for v in matches if cls._can_grab(v)]
        pool = grabbable or matches
        if len(pool) > 1:
            raise ValueError(f"Multiple V4L2 capture nodes match {key}={value}: {pool}")
        return pool[0]

    @classmethod
    def scan(cls):
        """Discover USB and non-USB V4L2 sources with capture modes and identifiers.

        Each reported node must advertise video capture, expose capture formats,
        and yield a packet through PyAV. Identifiers come from this backend's
        sysfs scan; CameraFinder is not needed for device resolution.
        """
        base = Path("/sys/class/video4linux")
        if not base.exists():
            return []
        id_map = {r["video_path"]: r for r in cls._list_identifiers()}
        cards = []
        for name in sorted(p.name for p in base.iterdir()):
            if not name.startswith("video"):
                continue
            vpath = f"/dev/{name}"
            info = cls._v4l2_device_info(vpath)
            if info is None or not info.get("is_capture"):
                continue
            if not info.get("modes"):        # metadata / control node: no capture formats
                continue
            if not cls._can_grab(vpath):      # declares capture but yields no frame
                continue
            ident = id_map.get(vpath, {})
            cards.append({
                "type": "v4l2",
                "video_path": vpath,
                "video_id": int(name.replace("video", "")),
                "physical_path": ident.get("physical_path"),
                "serial_number": ident.get("serial_number"),
                "bcd_device": ident.get("bcd_device"),
                "vid_pid": ident.get("vid_pid"),
                "name": ident.get("name"),
                "manufacturer": ident.get("manufacturer"),
                "modes": info.get("modes", []),
            })
        return cards

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Resolve one configured identifier and open the matching V4L2 camera.

        Priority is physical_path, serial_number, bcd_device, vid_pid, then
        video_id. A selected identifier does not fall back to the next one.
        Returns None if no match is found and raises ValueError if ambiguous.
        """
        video_id = server_cfg.get("video_id", 0)
        video_path = f"/dev/video{video_id}" if video_id is not None else None
        physical_path = str(server_cfg.get("physical_path")) if server_cfg.get("physical_path") else None
        serial_number = str(server_cfg.get("serial_number")) if server_cfg.get("serial_number") else None
        _bcd = server_cfg.get("bcd_device")
        bcd_device = f"{_bcd:04d}" if type(_bcd) is int else (str(_bcd) if _bcd else None)
        vid_pid = str(server_cfg.get("vid_pid")) if server_cfg.get("vid_pid") else None

        ids = cls._list_identifiers()

        if physical_path is not None:
            vpath = cls._resolve_vpath(ids, "physical_path", physical_path)
            if vpath is None:
                logger_mp.error(f"[Teleimager] Cannot find V4L2Camera for {cam_topic} with physical path {physical_path}")
                return None
            else:
                return cls(cam_topic, vpath, server_cfg)

        if serial_number is not None:
            vpath = cls._resolve_vpath(ids, "serial_number", serial_number)
            if vpath is None:
                logger_mp.error(f"[Teleimager] Cannot find V4L2Camera for {cam_topic} with serial number {serial_number}")
                return None
            return cls(cam_topic, vpath, server_cfg)

        if bcd_device is not None:
            vpath = cls._resolve_vpath(ids, "bcd_device", bcd_device)
            if vpath is None:
                logger_mp.error(f"[Teleimager] Cannot find V4L2Camera for {cam_topic} with bcd_device {bcd_device}")
                return None
            return cls(cam_topic, vpath, server_cfg)

        if vid_pid is not None:
            vpath = cls._resolve_vpath(ids, "vid_pid", vid_pid)
            if vpath is None:
                logger_mp.error(f"[Teleimager] Cannot find V4L2Camera for {cam_topic} with vid_pid {vid_pid}")
                return None
            return cls(cam_topic, vpath, server_cfg)

        if not any(r["video_path"] == video_path for r in ids):
            logger_mp.error(f"[Teleimager] Cannot find V4L2Camera for {cam_topic} with video_id {video_id}")
            return None
        return cls(cam_topic, video_path, server_cfg)
