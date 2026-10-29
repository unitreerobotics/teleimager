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

"""Capture JPEG frames from a GStreamer pipeline ending in an appsink."""

from pathlib import Path
import glob
import os
import re
import subprocess
import sys

import logging_mp

from .camera import BaseCamera, _turbojpeg

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


class GStreamerCamera(BaseCamera):
    """Read JPEG samples from one pipeline and decode BGR only when requested."""

    def __init__(self, cam_topic, gst_pipeline, server_cfg):
        """Start the supplied pipeline and verify that its appsink yields JPEG data."""
        super().__init__(cam_topic, server_cfg)
        self._Gst = self.get_gstreamer_module()
        self._pipeline_str = gst_pipeline

        # Multiple sources must be composed inside this single pipeline.
        self.pipe, self.sink = self._start_pipeline(self._pipeline_str)

        if not self._can_read_frame():
            self.release()
            raise RuntimeError(f"[GStreamerCamera] Camera {self._cam_topic} failed to initialize or read frames.")
        else:
            logger_mp.info(str(self))

    def __str__(self):
        zmq = f"zmq=on (port={self._zmq_port})" if self._enable_zmq else "zmq=off"
        webrtc = f"webrtc=on (port={self._webrtc_port})" if self._enable_webrtc else "webrtc=off"
        return f" 📷 Camera {self._cam_topic!r:<22} gstreamer, {self._img_shape[0]}x{self._img_shape[1]}@{self._fps}fps, {zmq}, {webrtc}"

    @staticmethod
    def get_gstreamer_module():
        # Add system GStreamer typelibs before importing the GI repository.
        """Load and initialize Gst, exiting with diagnostics if unavailable."""
        dirs = (glob.glob("/usr/lib/girepository-1.0")                  # Fedora/Arch, plain
                + glob.glob("/usr/lib/*linux-gnu*/girepository-1.0"))   # Debian/Ubuntu multiarch
        have = os.environ.get("GI_TYPELIB_PATH", "").split(":")
        add = [d for d in dirs if d not in have]
        if add:
            os.environ["GI_TYPELIB_PATH"] = ":".join([p for p in have if p] + add)
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            if not Gst.is_initialized():
                Gst.init(None)
            return Gst
        except Exception as e:
            if "Namespace" in str(e):
                # The distro typelib dirs were added above, so Gst is genuinely absent.
                logger_mp.error(
                    f"GStreamer typelibs missing ({e}).\n"
                    "  Install them system-wide:\n"
                    "      sudo apt install gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0"
                )
            else:
                # PyGObject must match the interpreter: conda ships its own, apt's serves the distro python.
                if os.path.exists(os.path.join(sys.prefix, "conda-meta")):
                    install = "      conda install -c conda-forge pygobject\n"
                elif any(p.endswith("/dist-packages") for p in sys.path):
                    install = "      sudo apt install python3-gi\n"
                else:
                    install = ("      pip install PyGObject\n"
                               "      (source build also needs: sudo apt install libgirepository1.0-dev libcairo2-dev)\n")
                logger_mp.error(
                    f"PyGObject/GStreamer not installed ({e}).\n"
                    f"  Interpreter: {sys.executable}\n"
                    "  [1/2] PyGObject — the Python binding, this interpreter only:\n"
                    f"{install}"
                    "  [2/2] GStreamer — typelibs + plugins, system-wide (always needed):\n"
                    "      sudo apt install gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-{base,good,bad}"
                )
            sys.exit(1)

    def _start_pipeline(self, pipeline_str):
        """Start the pipeline and return its single-buffer JPEG appsink."""
        Gst = self._Gst
        pipe = Gst.parse_launch(pipeline_str)
        # Prefer name=sink, otherwise use the first appsink element.
        sink = pipe.get_by_name("sink")
        if sink is None:
            it = pipe.iterate_sinks()
            while True:
                ok, elem = it.next()
                if ok != Gst.IteratorResult.OK:
                    break
                factory = elem.get_factory()
                if factory is not None and factory.get_name() == "appsink":
                    sink = elem
                    break
        if sink is None:
            raise RuntimeError(f"[GStreamerCamera] pipeline must contain an appsink (optionally name=sink): {pipeline_str}")

        sink.set_property("emit-signals", False)
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("caps", Gst.Caps.from_string("image/jpeg"))
        pipe.set_state(Gst.State.PLAYING)
        return pipe, sink

    def _pull_jpeg(self, sink, timeout_ns=1_000_000_000):
        """Return one copied JPEG sample, or None on timeout or buffer-map failure."""
        Gst = self._Gst
        sample = sink.emit("try-pull-sample", timeout_ns)
        if sample is None:
            return None
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            jpeg = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        return jpeg

    def _can_read_frame(self):
        """Allow up to five seconds for the first JPEG sample during source warmup."""
        return self._pull_jpeg(self.sink, timeout_ns=5_000_000_000) is not None

    def _update_frame(self):
        """Refresh JPEG and optional BGR buffers, raising if no sample arrives."""
        jpeg_bytes = self._pull_jpeg(self.sink)
        if jpeg_bytes is None:
            raise RuntimeError

        # Preserve encoded JPEG bytes for ZMQ to avoid re-encoding.
        if self._enable_zmq:
            self._zmq_buffer.write(jpeg_bytes)

        # Decode only when WebRTC or App streaming requests BGR output.
        if self._enable_bgr_output:
            self._bgr_buffer.write(_turbojpeg.decode(jpeg_bytes))

        if not self._ready.is_set():
            self._ready.set()

    def release(self):
        """Set the pipeline to NULL and clear its reference."""
        p = getattr(self, "pipe", None)
        if p is not None:
            try:
                p.set_state(self._Gst.State.NULL)
            except Exception:
                pass
        self.pipe = None
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @staticmethod
    def _get_devlinks_from_vpath(video_path):
        # Prefer stable udev paths: by-path identifies USB topology, while
        # by-id identifies the device by vendor, model, and serial number.
        """Find by-path and by-id udev links resolving to the supplied video node."""
        out = {"physical_path": None, "serial_number": None}
        if not video_path:
            return out
        try:
            target = Path(video_path).resolve()
        except Exception:
            return out
        for key, d in (("physical_path", Path("/dev/v4l/by-path")), ("serial_number", Path("/dev/v4l/by-id"))):
            if not d.exists():
                continue
            for link in sorted(d.iterdir()):
                try:
                    if link.resolve() == target:
                        out[key] = str(link)
                        break
                except Exception:
                    continue
        return out

    @classmethod
    def scan(cls):
        """Discover video sources and suggest JPEG appsink pipelines.

        Uses gst-device-monitor-1.0 and substitutes stable udev paths when found.
        Returns an empty list when the device monitor cannot be run.
        """
        try:
            r = subprocess.run(["gst-device-monitor-1.0", "Video/Source"], capture_output=True, text=True, timeout=5)
            txt = r.stdout or ""
        except Exception:
            return []
        cards = []
        name, device = None, None
        for line in txt.splitlines():
            s = line.strip()
            mname = re.match(r"name\s*:\s*(.+)", s)
            # The v4l2 provider prints device.path; a PipeWire-exposed one prints api.v4l2.path.
            mdev = re.match(r"(?:device\.path|api\.v4l2\.path)\s*=\s*(/dev/video\d+)", s)
            mlaunch = re.search(r"gst-launch-1\.0\s+(.+)", s)
            if mname:
                name = mname.group(1).strip()
            elif mdev:
                device = mdev.group(1)
            elif mlaunch:
                launch = mlaunch.group(1).strip()
                launch = re.sub(r"\s*!\s*\.\.\.\s*$", "", launch).rstrip("! ").strip()

                if "realsense" in (name or "").lower():
                    cards.append({
                        "type": "gstreamer",
                        "name": name,
                        "video_path": device,
                        "recommend_backend": "realsense",
                        "gst_pipeline": None,
                    })
                    name, device = None, None
                    continue
                # Prefer a replug-stable device path over the raw /dev/videoN.
                links = cls._get_devlinks_from_vpath(device)
                stable_device = links["physical_path"] or links["serial_number"] or device
                if stable_device:
                    if "device=" in launch:
                        launch = re.sub(r"device=\S+", f"device={stable_device}", launch, count=1)
                    elif re.match(r"^v4l2src\b", launch):
                        launch = re.sub(r"^v4l2src\b", f"v4l2src device={stable_device}", launch)
                    else:
                        launch = re.sub(r"^[^!]+", f"v4l2src device={stable_device}", launch, count=1)
                cards.append({
                    "type": "gstreamer",
                    "name": name,
                    "video_path": device,
                    "device_physical_path": links["physical_path"],
                    "device_serial_number": links["serial_number"],
                    "gst_pipeline": f"{launch} ! image/jpeg ! appsink name=sink",
                })
                name, device = None, None
        return cards

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        """Create a pipeline camera, or return None if gst_pipeline is missing."""
        gst_pipeline = server_cfg.get("gst_pipeline")
        if not gst_pipeline:
            logger_mp.error(f"[Teleimager] type 'gstreamer' for {cam_topic} requires a 'gst_pipeline' (must contain appsink).")
            return None
        return cls(cam_topic, gst_pipeline, server_cfg)
