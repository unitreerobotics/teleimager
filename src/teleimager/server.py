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

"""Capture camera frames and publish ZMQ, WebRTC, and App TCP streams."""

from pathlib import Path
import argparse
import functools
import os
import signal
import sys
import threading
import time

import logging_mp
import numpy as np

from .camera.camera import BaseCamera
from .camera_finder import CameraFinder
from .camera.gstreamer import GStreamerCamera
from .camera.isaacsim import IsaacSimCamera
from .camera.realsense import RealSenseCamera
from .camera.rtp import RtpCamera
from .camera.uvc import UVCCamera
from .camera.v4l2 import V4L2Camera
from .config import read_server_config
from .host import reload_uvcvideo_module, set_performance_mode, setup_permissions
from .transport import webrtc
from .transport.webrtc import WebRTC_PublisherManager
from .transport.zmq_publisher import ZMQ_PublisherManager
from .transport.zmq_request import ZMQ_Responser

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


CONFIG_DIR = webrtc.CONFIG_DIR
CERT_PEM_PATH = webrtc.CERT_PEM_PATH
KEY_PEM_PATH = webrtc.KEY_PEM_PATH


def load_server_config(cli_path=None, *, streaming_config=None):
    """Load the explicit path or TELEIMAGER_CONFIG server selection.

    Raises:
        ValueError: Neither a path nor the environment variable is supplied.
        SystemExit: Reading or resolving the selected configuration fails.
    """
    reference = cli_path or os.environ.get("TELEIMAGER_CONFIG")
    if not reference:
        raise ValueError("Select a server configuration with --config or TELEIMAGER_CONFIG.")
    path = Path(reference).expanduser().resolve()
    try:
        config = read_server_config(path, streaming_config=streaming_config)
        logger_mp.info(f"[Teleimager] Loaded server config from {path}")
        return config
    except Exception as e:
        logger_mp.error(f"[Teleimager] Failed to load configuration file at {path}: {e}")
        sys.exit(1)


class TeleImageServer:
    """Own camera capture workers and the configured streaming outputs.

    Construction opens active camera backends and the configuration responder.
    start() launches capture and output workers; stop() requests shutdown, and
    wait_until_stopped() waits for that request and performs cleanup.
    """

    def __init__(self, server_config, enable_isaacsim=False):
        """Initialize cameras, output managers, and optional App streaming.

        Args:
            server_config: Resolved native camera and streaming settings.
            enable_isaacsim: Use shared-memory cameras for every active topic.
        """
        webrtc.CERT_PEM_PATH = CERT_PEM_PATH
        webrtc.KEY_PEM_PATH = KEY_PEM_PATH
        webrtc._apply_webrtc_config(server_config)
        self._server_config = server_config
        self._enable_isaacsim = enable_isaacsim
        self._stop_event = threading.Event()
        self._cameras: dict[str, BaseCamera] = {}

        self._app_stream_config = (server_config or {}).get("app_stream", {})
        self._app_stream = None
        self._app_stream_topics = ()
        self._app_stream_roles = {}
        self._app_stream_layout = self._app_stream_config.get("layout", "default")
        self._camera_frame_times = {}
        if self._app_stream_config.get("enabled", False):
            from .transport.tcp_stream import TcpStreamServer, DEFAULT_STREAM_NAME

            if self._app_stream_layout not in ("default", "stereo_quad"):
                raise ValueError(f"Unknown App stream layout: {self._app_stream_layout}")
            if self._app_stream_layout == "stereo_quad":
                roles = self._app_stream_config.get("camera_topics", {
                    "head_stereo": "head_stereo_camera",
                    "head_mono": "head_mono_camera",
                    "head_left": "head_left_mono_camera",
                    "head_right": "head_right_mono_camera",
                    "wrist_left": "wrist_left_camera",
                    "wrist_right": "wrist_right_camera",
                })
                if not isinstance(roles, dict) or not roles:
                    raise TypeError("stereo_quad camera_topics must contain a camera-role mapping.")
                if set(roles) - {"head_mono", "head_stereo", "head_left", "head_right", "wrist_left", "wrist_right"}:
                    raise ValueError("stereo_quad camera_topics contains an unknown camera role.")
                if any(not isinstance(topic, str) or not topic for topic in roles.values()):
                    raise ValueError("stereo_quad camera_topics must map roles to non-empty topic names.")
                if len(set(roles.values())) != len(roles):
                    raise ValueError("stereo_quad camera_topics must use a distinct topic for each role.")
                cameras = self._server_config["camera"]
                single_heads = sum(roles.get(role) in cameras for role in ("head_mono", "head_stereo"))
                independent_eyes = any(roles.get(role) in cameras for role in ("head_left", "head_right"))
                if single_heads > 1 or (single_heads and independent_eyes):
                    raise ValueError("stereo_quad must select one head source: mono, stereo, or independent eyes.")
                self._app_stream_roles = dict(roles)
                self._app_stream_topics = tuple(topic for topic in roles.values() if topic in cameras)
            else:
                topics = self._app_stream_config.get(
                    "camera_topics", ("head_stereo_camera", "wrist_left_camera", "wrist_right_camera"),
                )
                if not isinstance(topics, (list, tuple)) or len(topics) not in (1, 3):
                    raise ValueError("default camera_topics must contain one or three cameras.")
                self._app_stream_topics = tuple(topics)
            self._app_stream = TcpStreamServer(
                control_host=self._app_stream_config.get("control_host", "0.0.0.0"),
                control_port=self._app_stream_config.get("control_port", 13579),
                video_config=self._app_stream_config,
                stream_name=self._app_stream_config.get(
                    "stream_name",
                    DEFAULT_STREAM_NAME,
                ),
            )

        if not self._enable_isaacsim and any(
            cfg.get("type", "uvc").lower() in ("uvc", "v4l2", "realsense")
            for cfg in self._server_config["camera"].values()
        ):
            reload_uvcvideo_module()

        self._responser = ZMQ_Responser(self._server_config)
        self._zmq_publisher_manager = ZMQ_PublisherManager.get_instance()
        self._webrtc_publisher_manager = WebRTC_PublisherManager.get_instance()
        self._publisher_threads = []  # Retain workers for bounded cleanup joins.

        try:
            # Open only cameras needed by an enabled output.
            for cam_topic, server_cfg in self._server_config["camera"].items():
                if not server_cfg.get("enable_zmq", False) and not server_cfg.get("enable_webrtc", False) and cam_topic not in self._app_stream_topics:
                    continue

                cam_type = server_cfg.get("type", "uvc").lower()
                if self._enable_isaacsim and cam_type!="isaacsim":
                    cam_type = "isaacsim"

                try:
                    if cam_type == "v4l2":
                        self._cameras[cam_topic] = V4L2Camera.from_config(cam_topic, server_cfg)
                    elif cam_type == "realsense":
                        self._cameras[cam_topic] = RealSenseCamera.from_config(cam_topic, server_cfg)
                    elif cam_type == "uvc":
                        self._cameras[cam_topic] = UVCCamera.from_config(cam_topic, server_cfg)
                    elif cam_type == "gstreamer":
                        self._cameras[cam_topic] = GStreamerCamera.from_config(cam_topic, server_cfg)
                    elif cam_type == "rtp":
                        self._cameras[cam_topic] = RtpCamera.from_config(cam_topic, server_cfg)
                    elif cam_type == "isaacsim":
                        self._cameras[cam_topic] = IsaacSimCamera.from_config(cam_topic, server_cfg)
                    else:
                        logger_mp.error(f"[Teleimager] Unknown camera type {cam_type} for {cam_topic}, skipping...")
                        continue
                except Exception as exc:
                    if cam_type == "rtp":
                        raise
                    if self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics:
                        raise
                    self._cameras[cam_topic] = None
                    logger_mp.warning(f"[App Stream] {cam_topic} initialization failed: {exc}; using nocarema tile.")

            for head_topic in ("head_mono_camera", "head_stereo_camera"):
                if (
                    self._server_config["camera"].get(head_topic, {}).get("enable_zmq", False)
                    and self._cameras.get(head_topic) is None
                ):
                    raise RuntimeError(f"Required ZMQ {head_topic} failed to initialize.")

            for camera_topic in self._app_stream_topics:
                camera = self._cameras.get(camera_topic)
                if camera is None:
                    if self._app_stream_layout == "stereo_quad":
                        logger_mp.warning(f"[App Stream] {camera_topic} unavailable; using nocarema tile.")
                        continue
                    raise ValueError(f"App stream camera topic {camera_topic!r} is not available.")
                camera.enable_bgr_output()
        except Exception:
            logger_mp.error("[Teleimager] Initialization failed", exc_info=True)
            self._clean_up()
            raise

        logger_mp.info("[Teleimager] has started, waiting for client connections...")

    def _update_frames(self, cam_topic: str, camera: BaseCamera):
        """Refresh one camera's buffers until capture stops or fails.

        Pace shared-memory sources explicitly; hardware backends block on capture.
        RTP timeouts retry, while unavailable stereo_quad cameras may leave the
        other streams running with a placeholder in the App preview.
        """
        try:
            interval = 1.0 / camera.get_fps() if camera._capture_requires_pacing else 0.0
            next_frame_time = time.monotonic()
            while not self._stop_event.is_set():
                try:
                    camera._update_frame()
                    self._camera_frame_times[cam_topic] = time.monotonic()
                except Exception as e:
                    logger_mp.error(f"[Image Server] Error updating frame for {cam_topic} camera: {e}")
                    if isinstance(camera, RtpCamera):
                        self._camera_frame_times.pop(cam_topic, None)
                        if isinstance(e, TimeoutError):
                            continue
                        break
                    if self._app_stream_layout == "stereo_quad" and cam_topic in self._app_stream_topics:
                        self._camera_frame_times.pop(cam_topic, None)
                        break
                    self._stop_event.set()
                    break
                if not interval:
                    continue
                next_frame_time += interval
                sleep_time = next_frame_time - time.monotonic()
                if sleep_time > 0:
                    self._stop_event.wait(sleep_time)
                else:
                    next_frame_time = time.monotonic()
        except Exception as e:
            logger_mp.error(f"[Image Server] Failed to update frames for {cam_topic} camera: {e}")
            if not isinstance(camera, RtpCamera) and (self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics):
                self._stop_event.set()

    def _zmq_pub(self, cam_topic: str, camera: BaseCamera):
        """Publish buffered JPEG bytes at the camera FPS until shutdown."""
        try:
            interval = 1.0 / camera.get_fps()
            next_frame_time = time.monotonic()

            while not self._stop_event.is_set():
                if self._app_stream_layout == "stereo_quad" and cam_topic in self._app_stream_topics:
                    if time.monotonic() - self._camera_frame_times.get(cam_topic, float("-inf")) >= 2.0:
                        self._stop_event.wait(interval)
                        continue
                jpeg_bytes = camera.get_jpeg_bytes()
                if jpeg_bytes is not None:
                    self._zmq_publisher_manager.publish(jpeg_bytes, camera.get_zmq_port())
                else:
                    if isinstance(camera, RtpCamera):
                        self._stop_event.wait(interval)
                        continue
                    logger_mp.warning(f"[Image Server] {cam_topic} returned no frame.")
                    if self._app_stream_layout == "stereo_quad" and cam_topic in self._app_stream_topics:
                        self._stop_event.wait(interval)
                        continue
                    self._stop_event.set()
                    break

                next_frame_time += interval
                sleep_time = next_frame_time - time.monotonic()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    next_frame_time = time.monotonic()
        except Exception as e:
            logger_mp.error(f"[Image Server] Failed to publish zmq frame from {cam_topic} camera.")
            if not isinstance(camera, RtpCamera) and (self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics):
                self._stop_event.set()

    def _webrtc_pub(self, cam_topic: str, camera: BaseCamera):
        """Submit buffered BGR arrays to WebRTC at the camera FPS."""
        try:
            interval = 1.0 / camera.get_fps()
            webrtc_codec = camera.get_webrtc_codec()
            next_frame_time = time.monotonic()
            while not self._stop_event.is_set():
                if self._app_stream_layout == "stereo_quad" and cam_topic in self._app_stream_topics:
                    if time.monotonic() - self._camera_frame_times.get(cam_topic, float("-inf")) >= 2.0:
                        self._stop_event.wait(interval)
                        continue
                bgr_frame = camera.get_bgr_frame()

                if bgr_frame is not None:
                    self._webrtc_publisher_manager.publish(bgr_frame, camera.get_webrtc_port(), codec_pref=webrtc_codec)
                else:
                    if isinstance(camera, RtpCamera):
                        self._stop_event.wait(interval)
                        continue
                    logger_mp.info(f"[Image Server] {cam_topic} returned no frame.")
                    if self._app_stream_layout == "stereo_quad" and cam_topic in self._app_stream_topics:
                        self._stop_event.wait(interval)
                        continue
                    self._stop_event.set()
                    break

                next_frame_time += interval
                sleep_time = next_frame_time - time.monotonic()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    next_frame_time = time.monotonic()
        except Exception as e:
            logger_mp.error(f"[Image Server] Failed to publish rtc frame from {cam_topic} camera.")
            if not isinstance(camera, RtpCamera) and (self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics):
                self._stop_event.set()

    def _app_stream_pub(self):
        """Compose and send App previews while a stream is requested.

        stereo_quad uses placeholders for missing or stale camera frames. The
        default layout waits until all selected cameras provide BGR data.
        """
        from .transport.tcp_stream import DEFAULT_WIDTH, DEFAULT_HEIGHT, compose_camera_output

        width = int(self._app_stream_config.get("width", DEFAULT_WIDTH))
        height = int(self._app_stream_config.get("height", DEFAULT_HEIGHT))

        while not self._stop_event.is_set():
            if not self._app_stream.stream_requested:
                self._stop_event.wait(0.1)
                continue
            if not self._app_stream.frame_due():
                self._stop_event.wait(0.005)
                continue

            frames_by_topic = {}
            for camera_topic in self._app_stream_topics:
                camera = self._cameras.get(camera_topic)
                frame = camera.get_bgr_frame() if camera is not None else None
                if self._app_stream_layout == "stereo_quad":
                    if time.monotonic() - self._camera_frame_times.get(camera_topic, float("-inf")) >= 2.0:
                        frame = None
                frames_by_topic[camera_topic] = frame
            if self._app_stream_layout == "stereo_quad":
                frames_by_role = {
                    role: frames_by_topic.get(topic) for role, topic in self._app_stream_roles.items()
                }
                for role in ("head_mono", "head_stereo"):
                    head_topic = self._app_stream_roles.get(role)
                    if head_topic not in self._server_config["camera"]:
                        continue
                    head = frames_by_role.get(role)
                    if head is not None and self._server_config["camera"][head_topic].get("binocular", False):
                        frames_by_role["head_left"], frames_by_role["head_right"] = np.hsplit(head, 2)
                    else:
                        frames_by_role["head_left"] = frames_by_role["head_right"] = head
                frames = [frames_by_role.get(role) for role in ("head_left", "head_right", "wrist_left", "wrist_right")]
            else:
                frames = [frames_by_topic[topic] for topic in self._app_stream_topics]
            if self._app_stream_layout == "default" and any(frame is None for frame in frames):
                self._stop_event.wait(0.01)
                continue

            try:
                output = compose_camera_output(
                    frames,
                    width=width,
                    height=height,
                    layout=self._app_stream_layout,
                )
                self._app_stream.send_frame(output)
            except Exception as exc:
                logger_mp.warning(f"[App Stream] Failed to publish frame: {exc}")
                self._stop_event.wait(0.1)

    def _clean_up(self):
        """Stop outputs, join workers briefly, and release camera resources."""
        if self._app_stream is not None:
            self._app_stream.stop()
        self._responser.stop()
        for t in self._publisher_threads:
            if t.is_alive():
                t.join(timeout=1.0)
        self._publisher_threads.clear()

        try:
            self._zmq_publisher_manager.close()
        except Exception:
            pass
        try:
            self._webrtc_publisher_manager.close()
        except Exception:
            pass

        for cam in self._cameras.values():
            if cam:
                try:
                    cam.release()
                except Exception as e:
                    logger_mp.error(f"[Image Server] Error releasing camera {cam._cam_topic}: {e}")
        logger_mp.info("[Image Server] Clean up completed. Server stopped.")

    # Public API.
    def start(self):
        """Launch capture workers, check readiness, and start enabled outputs.

        RTP sources and stereo_quad preview cameras do not block startup waiting
        for readiness. Other readiness failures request shutdown and clean up.
        """
        for camera_topic, camera in self._cameras.items():
            if camera is None:
                if self._app_stream_layout == "stereo_quad" and camera_topic in self._app_stream_topics:
                    continue
                logger_mp.error(f"[Image Server] Camera {camera_topic} failed to initialize previously, cannot start.")
                self._stop_event.set()
                self._clean_up()
                return
            t = threading.Thread(target=self._update_frames, args=(camera_topic, camera), daemon=True)
            t.start()
            self._publisher_threads.append(t)
        if self._enable_isaacsim:
            time.sleep(2.0)  # Allow the simulator to populate shared memory.

        for camera_topic, camera in self._cameras.items():
            if isinstance(camera, RtpCamera) or (self._app_stream_layout == "stereo_quad" and camera_topic in self._app_stream_topics):
                continue
            # Allow a longer readiness window when forcing simulator backends.
            if self._enable_isaacsim:
                timeout = 15.0
            else:
                timeout = 5.0
            ready = camera.wait_until_ready(timeout=timeout)
            if not ready:
                logger_mp.error(f"[Image Server] {camera_topic} ready timeout after {timeout}s.")
                self._stop_event.set()
                self._clean_up()
                return
            logger_mp.info(f"[Image Server] {camera_topic} is ready.")

        if self._app_stream is not None:
            self._app_stream.start()
            t = threading.Thread(target=self._app_stream_pub, daemon=True)
            t.start()
            self._publisher_threads.append(t)

        for camera_topic, camera in self._cameras.items():
            if camera is None:
                continue
            if camera.enable_webrtc():
                t = threading.Thread(target=self._webrtc_pub, args=(camera_topic, camera), daemon=True)
                t.start()
                self._publisher_threads.append(t)

            if camera.enable_zmq():
                t = threading.Thread(target=self._zmq_pub, args=(camera_topic, camera), daemon=True)
                t.start()
                self._publisher_threads.append(t)

    def wait_until_stopped(self):
        """Wait for a shutdown request, then release server resources."""
        self._stop_event.wait()
        self._clean_up()

    def stop(self):
        """Request shutdown without waiting for workers or performing cleanup."""
        self._stop_event.set()


def signal_handler(server, signum, frame):
    """Request server shutdown when a registered process signal arrives."""
    logger_mp.info(f"[Teleimager] Received signal {signum}, initiating graceful shutdown...")
    server.stop()


def run_isaacsim_server(config_path=None, *, streaming_config=None):
    """Load settings and return a started server using shared-memory cameras."""
    server_config = load_server_config(config_path, streaming_config=streaming_config)
    server = TeleImageServer(server_config, enable_isaacsim=True)
    server.start()
    return server


def main():
    # ANSI colors for --help, only when stdout is a real terminal (skipped when piped/redirected).
    """Run camera discovery, host setup, or streaming from command-line options."""
    _tty = sys.stdout.isatty()
    C = (lambda s, code: f"\033[{code}m{s}\033[0m" if _tty else s)
    head = lambda s: C(s, "1;36")   # bold cyan  — section / step titles
    cmd  = lambda s: C(s, "1;32")   # bold green — runnable commands
    flag = lambda s: C(s, "33")     # yellow     — flags / params
    dim  = lambda s: C(s, "2")      # dim        — notes / hints
    url  = lambda s: C(s, "4;34")   # underline blue — links

    guide = (
        f"{head('Getting started:')}\n"
        f"  Read the README first: {url('https://github.com/unitreerobotics/teleimager')}\n"
        "\n"
        f"  {head('Step 1')} — Discover connected cameras ('cf' = camera find):\n"
        f"      {cmd('teleimager-server')} {flag('--cf --uvc --v4l2 --gst --rs')}\n"
        f"    {dim('--cf by itself scans nothing. Add one flag per camera type you want to scan:')}\n"
        f"    {dim('  --uvc (USB UVC)  --v4l2 (V4L2)  --gst (GStreamer)  --rs (RealSense).')}\n"
        f"    {dim('Pass as many as you like; the example above scans all four backends.')}\n"
        "    The output lists each camera's video path, serial number, physical path, etc.\n"
        "\n"
        f"  {head('Step 2')} — Create a server YAML from the Camera Finder Report.\n"
        "    Select it explicitly with --config <path> or $TELEIMAGER_CONFIG.\n"
        "\n"
        f"  {head('Step 3')} — Start the server:\n"
        f"      {cmd('teleimager-server')} {flag('--config <server.yaml>')}\n"
        "\n"
        f"  {head('Step 4')} — Receive the streams:\n"
        f"      {flag('ZMQ')}    : {cmd('teleimager-client --host <server_ip>')}\n"
        f"      {flag('WebRTC')} : open {url('https://<server_ip>:<webrtc_port>')} in a browser\n"
    )
    parser = argparse.ArgumentParser(
        description="TeleImager: Unitree multi-camera image streaming service.",
        epilog=guide,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--setup-permissions', action='store_true', help='Grant camera access to non-root users (run once with sudo), then exit. Installs a udev rule, adds you to the video group, and allows the driver reload without a password.')
    parser.add_argument('--user', default=None, metavar='USER', help='(with --setup-permissions) Grant camera access to this account instead of $SUDO_USER. Needed from a root shell (`sudo su -`), where $SUDO_USER is unset.')
    parser.add_argument('--cf', action='store_true', help='Camera-finder mode: scan and print all connected cameras, then exit.')
    parser.add_argument('--uvc', action='store_true', help='In --cf, include the UVC (libuvc -> pyuvc) backend in the scan.')
    parser.add_argument('--v4l2', action='store_true', help='In --cf, include the V4L2 (PyAV) backend in the scan.')
    parser.add_argument('--gst', action='store_true', help='In --cf, include the GStreamer backend in the scan.')
    parser.add_argument('--rs', action='store_true', help='In --cf, include the RealSense (pyrealsense2) backend in the scan.')
    parser.add_argument('--isaacsim', action='store_true', help='Run the server in IsaacSim mode (frames from shared memory instead of physical cameras).')
    parser.add_argument('--no-affinity', action='store_false', dest='affinity', help='Do not pin the process to specific CPU cores.')
    parser.add_argument('--config', default=None, metavar='PATH', help='Path to a server YAML (overrides $TELEIMAGER_CONFIG); one of these is required.')
    parser.add_argument('--streaming-config', default=os.environ.get('TELEIMAGER_STREAMING_CONFIG'), metavar='PATH', help='Unified ZMQ, TCP and WebRTC output settings.')
    parser.add_argument('--cert', default=None, metavar='PATH', help='Path to the TLS certificate (overrides the default ~/.config/teleimager/cert.pem).')
    parser.add_argument('--key', default=None, metavar='PATH', help='Path to the TLS private key (overrides the default ~/.config/teleimager/key.pem).')
    args = parser.parse_args()

    # Host setup runs independently of server configuration and CPU affinity.
    if args.setup_permissions:
        sys.exit(setup_permissions(args.user))

    if not args.cf and not args.config and not os.environ.get("TELEIMAGER_CONFIG"):
        parser.error("select a server configuration with --config or TELEIMAGER_CONFIG")

    global CERT_PEM_PATH, KEY_PEM_PATH
    # Explicit TLS paths override the user configuration directory defaults.
    if args.cert:
        CERT_PEM_PATH = Path(args.cert).expanduser().resolve()
    if args.key:
        KEY_PEM_PATH = Path(args.key).expanduser().resolve()

    if args.affinity:
        set_performance_mode(cores=[0, 1, 2])

    if args.cf:
        CameraFinder(enable_uvc=args.uvc,
                     enable_v4l2=args.v4l2,
                     enable_gstreamer=args.gst,
                     enable_realsense=args.rs)
        sys.exit(0)

    server_config = load_server_config(args.config, streaming_config=args.streaming_config)

    # WebRTC needs a TLS cert/key pair; verify it up front so a missing cert fails
    # fast here instead of crashing the WebRTC thread on every frame.
    if any(cfg.get("enable_webrtc", False) for cfg in (server_config.get("camera") or {}).values()):
        for label, p in (("certificate", CERT_PEM_PATH), ("private key", KEY_PEM_PATH)):
            if not p.exists():
                logger_mp.error(
                    f"[Teleimager] TLS {label} not found at {p}. WebRTC needs a cert/key pair. Either:\n"
                    f"  1) Put cert.pem / key.pem in the default config dir:\n"
                    f"       mkdir -p {CONFIG_DIR}\n"
                    f"       openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout {CONFIG_DIR / 'key.pem'} -out {CONFIG_DIR / 'cert.pem'} -subj \"/CN=teleimager\"\n"
                    f"     (or copy existing ones: cp cert.pem key.pem {CONFIG_DIR}/)\n"
                    f"  2) Point at existing files on the CLI: teleimager-server --cert <path> --key <path>"
                )
                sys.exit(1)

    server = TeleImageServer(server_config, enable_isaacsim=args.isaacsim)
    server.start()

    # Signal handlers request shutdown; wait_until_stopped performs cleanup.
    signal.signal(signal.SIGINT, functools.partial(signal_handler, server))
    signal.signal(signal.SIGTERM, functools.partial(signal_handler, server))

    logger_mp.info("[Teleimager] Running... Press Ctrl+C to exit.")
    server.wait_until_stopped()


if __name__ == "__main__":
    main()
