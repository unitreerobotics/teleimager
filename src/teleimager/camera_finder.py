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

"""Scan camera backends: list USB cameras as data, or print a backend report."""

from dataclasses import dataclass
import importlib.util
import shutil
import time
from typing import Optional

import logging_mp

from .host import reload_uvcvideo_module
from .camera.gstreamer import GStreamerCamera
from .camera.realsense import RealSenseCamera
from .camera.uvc import UVCCamera
from .camera.v4l2 import V4L2Camera

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


@dataclass(frozen=True, kw_only=True)
class UsbCamera:
    """One USB camera this computer lists, as find_usb_cameras returns it.

    Attributes:
        driver_type: "uvc" or "realsense", the camera config's driver_type.
        name: Product name the device reports, or None.
        serial_number: USB or RealSense serial number, or None.
        physical_path: Sysfs device path that the uvc backend matches; None for
            RealSense.
        video_path: A video node that lists the MJPEG modes, such as
            "/dev/video0", or None; None for RealSense.
        mjpeg_modes: MJPEG modes as (width, height, fps), the ones the uvc
            backend can open, largest frame first; empty for RealSense.
    """

    driver_type: str
    name: Optional[str]
    serial_number: Optional[str]
    physical_path: Optional[str] = None
    video_path: Optional[str] = None
    mjpeg_modes: tuple = ()


def find_usb_cameras():
    """List this computer's USB cameras without streaming from any of them.

    UVC cameras come from sysfs, one per USB interface (its capture and metadata
    video nodes share it), in USB-port order. RealSense video nodes are never
    listed as UVC cameras; RealSense cameras come from the RealSense SDK when
    pyrealsense2 is installed, in SDK order after the UVC ones, and are left out
    otherwise. A UVC camera that a running server holds through libuvc has no
    video node and is not listed, so stop the server first.

    Returns:
        A list of UsbCamera.
    """
    realsense_paths = set(RealSenseCamera._list_video_paths())
    nodes = {}
    for video_path in UVCCamera._list_video_paths():
        if video_path not in realsense_paths:
            nodes.setdefault(UVCCamera._get_ppath_from_vpath(video_path), []).append(video_path)
    cameras = []
    for physical_path in sorted(nodes):
        attrs = UVCCamera._usb_attrs_from_ppath(physical_path)
        node_modes = {video_path: UVCCamera._list_mjpeg_modes(video_path) for video_path in nodes[physical_path]}
        modes = {mode for listed in node_modes.values() for mode in listed}
        cameras.append(UsbCamera(
            driver_type="uvc",
            name=attrs["name"],
            serial_number=attrs["serial_number"],
            physical_path=physical_path,
            video_path=next((path for path, listed in node_modes.items() if listed), None),
            mjpeg_modes=tuple(sorted(modes, key=lambda mode: (-mode[0] * mode[1], -mode[0], -mode[2]))),
        ))
    if importlib.util.find_spec("pyrealsense2") is not None:
        try:
            devices = RealSenseCamera._list_devices()
        except (Exception, SystemExit) as e:  # the SDK reports failures of its own kinds
            logger_mp.warning(f"[Teleimager] Cannot list RealSense cameras: {e}")
            devices = []
        cameras.extend(
            UsbCamera(driver_type="realsense", name=name, serial_number=serial_number)
            for serial_number, name in devices
        )
    return cameras


def read_brightness(camera, duration=0.8):
    """Return the mean brightness, 0-255, of a UVC camera's frames over duration seconds.

    The camera opens through its video node in its smallest MJPEG mode; frames
    of the first fifth of the time are skipped while exposure settles, and the
    result is the median of the remaining frames' means. Used to tell which
    camera a person covers. Returns None when the camera has no video node or
    MJPEG mode, OpenCV is missing, or no frame arrives.
    """
    if camera.video_path is None or not camera.mjpeg_modes:
        return None
    try:
        import cv2
    except ImportError:
        return None
    width, height, fps = min(camera.mjpeg_modes, key=lambda mode: (mode[0] * mode[1], -mode[2]))
    capture = cv2.VideoCapture(camera.video_path, cv2.CAP_V4L2)
    try:
        if not capture.isOpened():
            return None
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_FPS, fps)
        start = time.monotonic()
        means = []
        while (elapsed := time.monotonic() - start) < duration:
            grabbed, frame = capture.read()
            if grabbed and frame is not None and elapsed >= duration / 5:
                means.append(float(frame.mean()))
    finally:
        capture.release()
    if not means:
        return None
    means.sort()
    return means[len(means) // 2]


class CameraFinder:
    """Discover camera capabilities and render a backend-specific report."""

    def __init__(self, enable_uvc=False, enable_v4l2=False, enable_gstreamer=False, enable_realsense=False):
        """Select backends and immediately scan and print their reports."""
        self.enable_uvc = enable_uvc
        self.enable_v4l2 = enable_v4l2
        self.enable_gstreamer = enable_gstreamer
        self.enable_realsense = enable_realsense
        self.report()

    def report(self):
        """Scan selected backends and log a tree of identifiers and modes.

        Reloads the UVC driver for UVC, V4L2, or GStreamer scans. RealSense nodes
        are filtered per backend before rendering the discovery results.
        """
        if self.enable_uvc or self.enable_v4l2 or self.enable_gstreamer:
            reload_uvcvideo_module()

        rs_cards = RealSenseCamera.scan() if self.enable_realsense else []
        rs_video_paths = set(RealSenseCamera._list_video_paths())
        rs_depth_ir_paths = set(RealSenseCamera._list_depth_ir_paths())

        uvc_cards = [c for c in UVCCamera.scan() if c["video_path"] not in rs_video_paths] if self.enable_uvc else []
        v4l2_cards = [c for c in V4L2Camera.scan() if c["video_path"] not in rs_depth_ir_paths] if self.enable_v4l2 else []
        gst_cards = [c for c in GStreamerCamera.scan() if c["video_path"] not in rs_depth_ir_paths] if self.enable_gstreamer else []

        branches = []
        if self.enable_realsense:
            branches.append(self._report_realsense(rs_cards))
        if self.enable_uvc:
            branches.append(self._report_uvc(uvc_cards))
        if self.enable_v4l2:
            branches.append(self._report_v4l2(v4l2_cards))
        if self.enable_gstreamer:
            branches.append(self._report_gstreamer(gst_cards))

        logger_mp.info("[Teleimager] 🎯 Camera Finder Report")
        self._print_children(branches, "")
        if not any([self.enable_uvc, self.enable_v4l2, self.enable_gstreamer, self.enable_realsense]):
            logger_mp.warning(
                "[Teleimager] 🖼️  No camera backends enabled — nothing to scan.  Add one flag per camera type:\n"
                "    teleimager-server --cf --uvc --v4l2 --gst --rs\n"
                "    --uvc (USB UVC)  --v4l2 (V4L2)  --gst (GStreamer)  --rs (RealSense)")
        elif rs_cards or uvc_cards or v4l2_cards or gst_cards:
            logger_mp.info("[Teleimager] 👉 Use the results above to edit your server config yaml.")
        else:
            # A backend ran but found nothing, so there is nothing to configure.
            logger_mp.warning("[Teleimager] 👉 No cameras found — check the camera is connected and the driver is loaded.")

    @staticmethod
    def _print_children(children, prefix):
        """Recursively log report branches using tree indentation."""
        n = len(children)
        for i, (label, sub) in enumerate(children):
            last = i == n - 1
            logger_mp.info("%s%s%s", prefix, "└─ " if last else "├─ ", label)
            if sub:
                CameraFinder._print_children(sub, prefix + ("   " if last else "│  "))

    @staticmethod
    def _leaf(text):
        """Return a report node without children."""
        return (text, [])

    def _report_realsense(self, cards):
        """Build the RealSense branch from serial numbers and color modes."""
        cams = []
        for c in cards:
            sn = c.get("serial_number") or "(none)"
            fields = [self._leaf("serial_number : %s" % sn)]
            modes = c.get("modes") or []
            if modes:
                # Group the color modes exposed by the RealSense scan.
                from collections import defaultdict
                by_stream = defaultdict(list)
                for m in modes:
                    by_stream[m["stream"]].append(m)
                stream_kids = []
                for stream in sorted(by_stream):
                    mode_kids = []
                    for m in sorted(by_stream[stream], key=lambda x: (x["format"], x["width"], x["height"])):
                        fps_str = ", ".join(str(f) for f in m["fps"])
                        mode_kids.append(self._leaf("%-6s %dx%d @ [%s]" % (m["format"], m["width"], m["height"], fps_str)))
                    stream_kids.append((stream, mode_kids))
                fields.append(("modes  [format  width x height @ fps]:", stream_kids))
            cams.append(("📷 RealSense", fields))
        return ("RealSenseCamera (%d found)   [driver_type: realsense]" % len(cams), cams)

    def _report_uvc(self, cards):
        """Build the UVC branch, querying libuvc for each camera's modes."""
        cams = []
        for cam in cards:
            dev_info = cam.get("dev_info") or {}
            uid = cam.get("uid") or "?"
            vid = dev_info.get("idVendor") or "?"
            pid = dev_info.get("idProduct") or "?"
            bcd = cam.get("bcd_device") or "?"
            sn = cam.get("serial_number") or "(none)"
            name = dev_info.get("name") or "?"
            mfr = dev_info.get("manufacturer") or "?"

            if isinstance(vid, int) and isinstance(pid, int):
                vidpid_str = f"{vid:04x}:{pid:04x}"
            else:
                vidpid_str = f"{vid}:{pid}"

            fields = [
                self._leaf("%-14s: %s" % ("physical_path", cam.get("physical_path"))),
                self._leaf("%-14s: %s" % ("serial_number", sn)),
                self._leaf("%-14s: %-15s(USB device release number)" % ("bcdDevice", bcd)),
                self._leaf("%-14s: %-15s(VendorID : ProductID)" % ("vid : pid", vidpid_str)),
                self._leaf("%-14s: %-15s(/dev/video%s)" % ("video_id", cam.get("video_id"), cam.get("video_id"))),
            ]

            # Group mode FPS values by resolution.
            try:
                uvc = UVCCamera.get_uvc_module()
                cap = uvc.Capture(uid)
                from collections import defaultdict
                by_res = defaultdict(list)
                fmt_names = set()
                for m in cap.available_modes:
                    by_res[(m.width, m.height)].append(m.fps)
                    fmt_names.add(m.format_name)
                cap.close()

                mode_kids = []
                for (w, h), fps_list in sorted(by_res.items()):
                    fps_str = ", ".join(str(f) for f in sorted(fps_list))
                    mode_kids.append(self._leaf("%dx%d @ [%s]" % (w, h, fps_str)))
                fmt_str = ", ".join(sorted(fmt_names))
                fields.append(("modes (%s)  [width x height @ fps]:" % fmt_str, mode_kids))
            except Exception as e:
                # PyAV probing during scan does not guarantee libuvc access.
                # Keep identifiers visible when USB access or mode queries fail.
                if "denied" in str(e).lower():
                    fields.append(self._leaf(
                        "⚠️  modes unavailable: USB access denied, run: "
                        'sudo "$(which teleimager-server)" --setup-permissions'))
                else:
                    fields.append(self._leaf("⚠️  modes unavailable: %s" % e))

            cams.append(("📷 %s (%s)" % (name, mfr), fields))
        return ("UVCCamera (%d found)   [driver_type: uvc]" % len(cams), cams)

    def _report_v4l2(self, cards):
        # Distinguish a missing discovery tool from an empty device list.
        """Build the V4L2 branch, reporting a missing v4l2-ctl explicitly."""
        if not shutil.which("v4l2-ctl"):
            return ("V4L2Camera (0 found)   [driver_type: v4l2]",
                    [self._leaf("⚠️  v4l2-ctl not installed, run: sudo apt install -y v4l-utils")])
        cams = []
        for c in cards:
            fields = [
                self._leaf("%-14s: %s" % ("physical_path", c.get("physical_path"))),
                self._leaf("%-14s: %s" % ("serial_number", c.get("serial_number") or "(none)")),
                self._leaf("%-14s: %-15s(USB device release number)" % ("bcdDevice", c.get("bcd_device") or "?")),
                self._leaf("%-14s: %-15s(VendorID : ProductID)" % ("vid : pid", c.get("vid_pid") or "(none)")),
                self._leaf("%-14s: %-15s(/dev/video%s)" % ("video_id", c.get("video_id"), c.get("video_id"))),
            ]
            modes = c.get("modes") or []
            if modes:
                mode_kids = []
                for m in modes:
                    fps_str = ", ".join(str(f) for f in m.get("fps", []))
                    mode_kids.append(self._leaf("%-6s %dx%d @ [%s]" % (
                        m.get("format"), m.get("width"), m.get("height"), fps_str)))
                fields.append(("modes  [width x height @ fps]:", mode_kids))

            name = c.get("name") or c["video_path"]
            mfr = c.get("manufacturer") or "?"
            cams.append(("📷 %s (%s)" % (name, mfr), fields))
        return ("V4L2Camera (%d found)   [driver_type: v4l2]" % len(cams), cams)

    def _report_gstreamer(self, cards):
        """Build the GStreamer branch with stable device paths and a usage note."""
        cams = []
        for c in cards:
            fields = []
            if c.get("device_physical_path"):
                fields.append(self._leaf("device (physical_path) : %s" % c["device_physical_path"]))
            if c.get("device_serial_number"):
                fields.append(self._leaf("device (serial_number) : %s" % c["device_serial_number"]))
            cams.append(("📷 " + (c.get("name") or "?"), fields))
        children = cams
        if cams:
            children = cams + [
                self._leaf("⚠️  NOTE: GStreamer is intended for GMSL/MIPI and other non-USB cameras; "
                           "for USB cameras prefer uvc or v4l2; build gst_pipeline with AI assistance."),
            ]
        return ("GStreamerCamera (%d found)   [driver_type: gstreamer]" % len(cams), children)
