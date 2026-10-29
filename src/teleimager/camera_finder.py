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

"""Scan selected camera backends and print their identifiers and modes."""

import shutil

import logging_mp

from .host import reload_uvcvideo_module
from .camera.gstreamer import GStreamerCamera
from .camera.realsense import RealSenseCamera
from .camera.uvc import UVCCamera
from .camera.v4l2 import V4L2Camera

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


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
        if any([self.enable_uvc, self.enable_v4l2, self.enable_gstreamer, self.enable_realsense]):
            logger_mp.info("[Teleimager] 👉 Use the results above to edit your server config yaml.")
        else:
            logger_mp.warning("[Teleimager] 🖼️ No camera backends enabled — nothing to scan.")
            logger_mp.info("[Teleimager] 👉 Add one flag per camera type you want to scan:")
            logger_mp.info("[Teleimager]      teleimager-server --cf --uvc --v4l2 --gst --rs")
            logger_mp.info("[Teleimager]      --uvc (USB UVC)   --v4l2 (V4L2)   --gst (GStreamer)   --rs (RealSense)")

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
        return ("RealSenseCamera (%d found)   [type: realsense]" % len(cams), cams)

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
        return ("UVCCamera (%d found)   [type: uvc]" % len(cams), cams)

    def _report_v4l2(self, cards):
        # Distinguish a missing discovery tool from an empty device list.
        """Build the V4L2 branch, reporting a missing v4l2-ctl explicitly."""
        if not shutil.which("v4l2-ctl"):
            return ("V4L2Camera (0 found)   [type: v4l2]",
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
        return ("V4L2Camera (%d found)   [type: v4l2]" % len(cams), cams)

    def _report_gstreamer(self, cards):
        """Build the GStreamer branch with pipeline and device suggestions."""
        cams = []
        for c in cards:
            fields = []
            if c.get("recommend_backend"):
                fields.append(self._leaf("recommend use type: %s (the gstreamer backend can't drive it)" % c["recommend_backend"]))
                cams.append(("📷 " + (c.get("name") or "?"), fields))
                continue
            if c.get("device_physical_path"):
                fields.append(self._leaf("device (physical_path) : %s" % c["device_physical_path"]))
            if c.get("device_serial_number"):
                fields.append(self._leaf("device (serial_number) : %s" % c["device_serial_number"]))
            fields.append(self._leaf('gst_pipeline: "%s"' % c.get("gst_pipeline")))
            cams.append(("📷 " + (c.get("name") or "?"), fields))
        return ("GStreamerCamera (%d found)   [type: gstreamer]" % len(cams), cams)
