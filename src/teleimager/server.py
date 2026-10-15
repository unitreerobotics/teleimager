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
import logging_mp
logging_mp.basicConfig(level=logging_mp.INFO)
logger_mp = logging_mp.getLogger(__name__)
import os
import sys
import argparse
import glob
import numpy as np
try:
    import av
    from aiohttp import web
    from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
    from aiortc.rtcrtpsender import RTCRtpSender
    from aiortc.contrib.media import MediaRelay
    from aiortc.codecs import h264
except ImportError as e:
    logger_mp.error(
        f"[Teleimager] Server dependencies not installed (missing '{e.name}').\n"
        "    The server needs the `[server]` extra (av / aiortc / aiohttp). Install with:\n"
        "        pip install \"teleimager[server]\"   # or [uvc] / [realsense] / [all]"
    )
    sys.exit(1)
import yaml
import time
import threading
import signal
import functools
import grp
import pwd
import re
import shutil
import subprocess
import selectors
import socket
from aiortc.codecs.h264 import H264Decoder, h264_depayload
from aiortc.jitterbuffer import JitterBuffer
from aiortc.rtp import RtpPacket
from av.video.reformatter import VideoReformatter
import tempfile
import platform
from .client import TripleRingBuffer, ZMQ_PublisherManager, ZMQ_Responser
# Shared JPEG codec (libturbojpeg): encode(BGR)->jpeg bytes, decode(jpeg)->BGR.
from turbojpeg import TurboJPEG
_turbojpeg = TurboJPEG()
import asyncio
import json
import ssl
from pathlib import Path
import queue
import fractions
from typing import Dict, Optional, Tuple, Any


# ========================================================
# teleimager_server.yaml path + TLS cert/key defaults
# ========================================================
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "teleimager"
SERVER_CONFIG_PATH = str(CONFIG_DIR / "teleimager_server.yaml")
CERT_PEM_PATH = CONFIG_DIR / "cert.pem"
KEY_PEM_PATH = CONFIG_DIR / "key.pem"

def load_server_config(cli_path=None):
    """Resolve the server config path, seeding the default from the bundled
    template if needed, then load and return the parsed yaml dict. Exits on failure."""
    if cli_path:
        path = Path(cli_path).expanduser().resolve()
    elif os.environ.get("TELEIMAGER_CONFIG"):
        path = Path(os.environ["TELEIMAGER_CONFIG"]).expanduser().resolve()
    else:
        path = Path(SERVER_CONFIG_PATH)
        if not path.exists():
            # First run: copy the bundled default template into the user config dir.
            try:
                from importlib import resources
                template = resources.files("teleimager").joinpath("teleimager_server.yaml").read_text(encoding="utf-8")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(template, encoding="utf-8")
                logger_mp.info(
                    f"[Teleimager] Created default config at {path}\n"
                    "  Next:\n"
                    "    1) Discover your cameras (see 'teleimager-server --help').\n"
                    "    2) Edit the config file above per the discovery results.\n"
                    "    3) Start the server: teleimager-server"
                )
            except Exception as e:
                logger_mp.error(f"[Teleimager] Failed to create default config at {path}: {e}")
                sys.exit(1)
    try:
        with open(path, "r") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict) or not isinstance(config.get("camera"), dict):
            raise ValueError(
                "TeleImager 2.0 requires camera topics below 'camera:'. "
                "Migrate the old top-level camera configuration first."
            )
        logger_mp.info(f"[Teleimager] Loaded server config from {path}")
        return config
    except Exception as e:
        logger_mp.error(f"[Teleimager] Failed to load configuration file at {path}: {e}")
        sys.exit(1)


# ========================================================
# WebRTC global encoder config (bitrate caps + GOP).
# aiortc's bitrate constants are module-level, so this is global by design.
# ========================================================
_GOP_LENGTH = 60  # frames between keyframes; overridden by yaml if present

def _apply_webrtc_config(server_config):
    global _GOP_LENGTH
    cfg = (server_config or {}).get("webrtc", {})
    bitrate = cfg.get("bitrate", {})
    from aiortc.codecs import vpx
    for key, attr in (("min", "MIN_BITRATE"), ("default", "DEFAULT_BITRATE"), ("max", "MAX_BITRATE")):
        if key in bitrate and int(bitrate[key]) >= 100_000:
            setattr(h264, attr, int(bitrate[key]))
            setattr(vpx, attr, int(bitrate[key]))
    if "gop_length" in cfg and int(cfg["gop_length"]) > 0:
        _GOP_LENGTH = int(cfg["gop_length"])
    logger_mp.info(f"[Teleimager] bitrate min/default/max={h264.MIN_BITRATE}/{h264.DEFAULT_BITRATE}/{h264.MAX_BITRATE}, gop={_GOP_LENGTH}")

# ========================================================
# libx264 for Jetson (Patch h264 Encoder)
# ========================================================
def jetson_software_encode_frame(self, frame: av.VideoFrame, force_keyframe: bool):
    if self.codec and (frame.width != self.codec.width or frame.height != self.codec.height):
        self.codec = None

    if self.codec is None:
        try:
            self.codec = av.CodecContext.create("libx264", "w")
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.bit_rate = self.target_bitrate
            self.codec.pix_fmt = "yuv420p"
            self.codec.framerate = fractions.Fraction(30, 1)
            self.codec.time_base = fractions.Fraction(1, 30)

            self.codec.options = {
                "preset": "ultrafast",
                "tune": "zerolatency",
                "threads": "1",
                "g": str(_GOP_LENGTH),
            }
            self.frame_count = 0
            force_keyframe = True
        except Exception as e:
            logger_mp.error(f"[Teleimager] Initialization failed: {e}")
            return

    if not force_keyframe and hasattr(self, "frame_count") and self.frame_count % _GOP_LENGTH == 0:
        force_keyframe = True

    self.frame_count = self.frame_count + 1 if hasattr(self, "frame_count") else 1
    frame.pict_type = av.video.frame.PictureType.I if force_keyframe else av.video.frame.PictureType.NONE

    try:
        for packet in self.codec.encode(frame):
            data = bytes(packet)
            if data:
                yield from self._split_bitstream(data)
    except Exception as e:
        logger_mp.warning(f"[Teleimager] Encode error: {e}")

h264.H264Encoder._encode_frame = jetson_software_encode_frame

# ========================================================
# Embed HTML and JS directly
# ========================================================
with open(Path(__file__).resolve().with_name("unitree_logo.svg"), "r", encoding="utf-8") as f:
    UNITREE_LOGO_SVG = f.read()

INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>WebRTC Stream</title>
    <style>
    body {
        font-family: sans-serif;
        background: #fff;
        color: #000;
        text-align: center;
    }
    button { padding: 10px 20px; font-size: 16px; cursor: pointer; }
    video { width: 100%; max-width: 1280px; background: #000; margin-top: 10px; }

    /* Title link style */
    h1 a {
        text-decoration: none;
        color: #000;
    }
    h1 a:hover {
        color: #555;
    }
    .brand-logo svg {
        width: min(180px, 40vw);
        height: auto;
        display: block;
        margin: 0 auto;
    }
    </style>
</head>
<body>
    <h1>
        <a href="https://github.com/unitreerobotics/teleimager" target="_blank">
            Teleimager · WebRTC Stream
        </a>
    </h1>

    <div class="brand-logo" style="margin-bottom: 20px;">
        <a href="https://www.unitree.com/" target="_blank">
            __UNITREE_LOGO_SVG__
        </a>
    </div>

    <button id="start" onclick="start()">Start</button>
    <button id="stop" style="display: none" onclick="stop()">Stop</button>

    <div id="media">
        <video id="video" autoplay playsinline muted></video>
        <audio id="audio" autoplay></audio>
    </div>

    <script src="client.js"></script>
</body>
</html>
""".replace("__UNITREE_LOGO_SVG__", UNITREE_LOGO_SVG)

CLIENT_JS = """
var pc = null;
var _fallback = false;

function negotiate(codec) {
    pc.addTransceiver('video', { direction: 'recvonly' });
    return pc.createOffer().then((offer) => {
        return pc.setLocalDescription(offer);
    }).then(() => {
        return new Promise((resolve) => {
            if (pc.iceGatheringState === 'complete') {
                resolve();
            } else {
                const checkState = () => {
                    if (pc.iceGatheringState === 'complete') {
                        pc.removeEventListener('icegatheringstatechange', checkState);
                        resolve();
                    }
                };
                pc.addEventListener('icegatheringstatechange', checkState);
            }
        });
    }).then(() => {
        var offer = pc.localDescription;
        return fetch('/offer', {
            body: JSON.stringify({
                sdp: offer.sdp,
                type: offer.type,
                codec: codec || null
            }),
            headers: {
                'Content-Type': 'application/json'
            },
            method: 'POST'
        });
    }).then((response) => {
        return response.json();
    }).then((answer) => {
        return pc.setRemoteDescription(answer);
    }).catch((e) => {
        alert(e);
    });
}

function start() {
    var config = {
        sdpSemantics: 'unified-plan'
    };

    pc = new RTCPeerConnection(config);

    pc.addEventListener('track', (evt) => {
        if (evt.track.kind == 'video') {
            var v = document.getElementById('video');
            v.srcObject = evt.streams[0];
            // H.264 -> VP8 fallback: if no frames in 5s, reconnect with VP8
            if (!_fallback) {
                var t0 = v.currentTime;
                setTimeout(function() {
                    if (v.currentTime === t0 && !_fallback) {
                        stop();
                        _fallback = true;
                        start();
                    }
                }, 5000);
            }
        } else {
            document.getElementById('audio').srcObject = evt.streams[0];
        }
    });

    document.getElementById('start').style.display = 'none';
    negotiate(_fallback ? 'vp8' : null);
    document.getElementById('stop').style.display = 'inline-block';
}

function stop() {
    document.getElementById('stop').style.display = 'none';
    document.getElementById('start').style.display = 'inline-block';
    if (pc) {
        pc.close();
        pc = null;
    }
    _fallback = false;
}
"""

# ========================================================
# WebRTC publish
# ========================================================
class BGRArrayVideoStreamTrack(MediaStreamTrack):
    """MediaStreamTrack exposing BGR ndarrays as av.VideoFrame (latest-frame semantics)."""
    kind = "video"

    def __init__(self):
        super().__init__()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._start_time = None
        self._pts = 0

    async def recv(self) -> av.VideoFrame:
        # This will suspend execution until a frame is available
        # preventing CPU busy-waiting
        frame = await self._queue.get()
        return frame

    def push_frame(self, bgr_numpy: np.ndarray, loop: Optional[asyncio.AbstractEventLoop] = None):
        if bgr_numpy is None:
            return

        # 1. Convert and calculate PTS immediately
        # MediaRelay requires consistent PTS to function correctly
        try:
            video_frame = av.VideoFrame.from_ndarray(bgr_numpy, format="bgr24")

            if self._start_time is None:
                self._start_time = time.time()
                self._pts = 0
            else:
                # 90000 is the standard RTP clock rate for video
                # This ensures smooth playback
                self._pts = int((time.time() - self._start_time) * 90000)

            video_frame.pts = self._pts
            video_frame.time_base = fractions.Fraction(1, 90000)

        except Exception as e:
            logger_mp.debug(f"[Teleimager] Conversion failed: {e}")
            return

        # 2. Push to queue thread-safely
        target_loop = loop or asyncio.get_event_loop()
        if target_loop.is_closed():
            return

        def _put():
            try:
                # Drop old frame if queue is full (Low Latency strategy)
                if self._queue.full():
                    self._queue.get_nowait()
                self._queue.put_nowait(video_frame)
            except Exception:
                pass

        target_loop.call_soon_threadsafe(_put)


class WebRTC_PublisherThread(threading.Thread):
    """
    Runs aiohttp + aiortc in a separate THREAD (not Process).
    This enables shared memory and removes Pickling overhead.
    """
    def __init__(self, port: int, host: str = "0.0.0.0", codec_pref: str = None):
        super().__init__(daemon=True)
        self._host = host
        self._port = port
        self._codec_pref = codec_pref
        self._app = web.Application()
        self._runner: Optional[web.AppRunner] = None
        self._pcs = set()
        self._start_event = threading.Event()
        self._stop_event = threading.Event()
        self._frame_queue = queue.Queue(maxsize=1)

        self._bgr_track: Optional[BGRArrayVideoStreamTrack] = None
        self._relay: Optional[MediaRelay] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # register routes
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/client.js", self._javascript)
        self._app.router.add_post("/offer", self._offer)

        self._app.router.add_options("/", self._options)
        self._app.router.add_options("/client.js", self._options)
        self._app.router.add_options("/offer", self._options)

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(content_type="text/html", text=INDEX_HTML)

    async def _javascript(self, request: web.Request) -> web.Response:
        return web.Response(content_type="application/javascript", text=CLIENT_JS)

    async def _options(self, request):
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )

    def _error_response(self, status: int, message: str) -> web.Response:
        return web.Response(
            status=status,
            content_type="application/json",
            text=json.dumps({"error": message}),
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )

    async def _offer(self, request: web.Request) -> web.Response:
        try:
            params = await request.json()
        except Exception:
            return self._error_response(400, "Invalid JSON body")

        # Reject malformed offers (e.g. scanners or non-conforming clients)
        # instead of letting them raise unhandled exceptions and spam logs.
        if not isinstance(params, dict) or "sdp" not in params or "type" not in params:
            logger_mp.warning(f"[Teleimager] Rejected malformed offer (missing sdp/type) for port:{self._port}")
            return self._error_response(400, "Missing 'sdp' or 'type'")

        user_agent = request.headers.get("User-Agent", "").lower()
        is_firefox = "firefox" in user_agent and "chrome" not in user_agent

        offer_sdp = params["sdp"]
        if is_firefox and ".local" in offer_sdp:
            # Keep ICE open when aioice cannot resolve Firefox mDNS candidates.
            offer_sdp = re.sub(r"a=end-of-candidates\s*\r?\n", "", offer_sdp)
        offer = RTCSessionDescription(sdp=offer_sdp, type=params["type"])

        pc = RTCPeerConnection()
        self._pcs.add(pc)

        # CORE LOGIC: Use MediaRelay to subscribe
        # This ensures encoding happens only once globally
        if self._bgr_track and self._relay:
            try:
                relayed_track = self._relay.subscribe(self._bgr_track)
                transceiver = pc.addTransceiver(relayed_track, direction="sendonly")
                capabilities = RTCRtpSender.getCapabilities("video")
                client_codec = params.get("codec")
                pref = (client_codec or self._codec_pref or "h264").lower()

                if pref == "h264":
                    h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
                    if h264_codecs:
                        transceiver.setCodecPreferences(h264_codecs)
                        logger_mp.info(f"[Teleimager] Preferred H264 for port:{self._port}")
                    else:
                        logger_mp.warning(f"[Teleimager] H264 preferred but not found, using auto-negotiation for port:{self._port}")

                elif pref == "vp8":
                    vp8_codecs = [c for c in capabilities.codecs if c.mimeType == "video/VP8"]
                    if vp8_codecs:
                        transceiver.setCodecPreferences(vp8_codecs)
                        logger_mp.info(f"[Teleimager] Preferred VP8 for port:{self._port}")
                    else:
                        logger_mp.warning(f"[Teleimager] VP8 preferred but not found, using auto-negotiation for port:{self._port}")

                else:
                    h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
                    if h264_codecs:
                        transceiver.setCodecPreferences(h264_codecs)
                        logger_mp.info(f"[Teleimager] Preferred codec '{pref}' not found, falling back to H264 for port:{self._port}")
                    else:
                        logger_mp.warning(f"[Teleimager] Preferred codec '{pref}' not found, using auto-negotiation for port:{self._port}")

            except Exception as e:
                logger_mp.error(f"[Teleimager] Relay subscription failed: {e}")

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            if pc.connectionState in ["failed", "closed"]:
                await self._cleanup_pc(pc)

        try:
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
        except Exception as e:
            # Malformed SDP (missing ICE ufrag/pwd) or codec negotiation failure
            # (e.g. client offers no codec compatible with our H264 preference).
            logger_mp.warning(f"[Teleimager] Negotiation failed for port:{self._port}: {e}")
            await self._cleanup_pc(pc)
            return self._error_response(400, f"Negotiation failed: {e}")

        return web.Response(
            content_type="application/json",
            text=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}),
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )

    async def _cleanup_pc(self, pc):
        self._pcs.discard(pc)
        try:
            await pc.close()
        except: pass

    def wait_for_start(self, timeout=1.0):
        return self._start_event.wait(timeout=timeout)

    def run(self):
        # Create a new Event Loop for this thread
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _main():
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()

            # Init Track and Relay inside the loop
            self._bgr_track = BGRArrayVideoStreamTrack()
            self._relay = MediaRelay()

            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(CERT_PEM_PATH, KEY_PEM_PATH)
            site = web.TCPSite(self._runner, self._host, self._port, ssl_context=ssl_context)
            await site.start()
            self._start_event.set()

            # Frame Pushing Loop
            while not self._stop_event.is_set():
                try:
                    # Non-blocking check for new frames
                    if not self._frame_queue.empty():
                        # Get frame (no pickling overhead in Threads!)
                        frame = self._frame_queue.get_nowait()
                        self._bgr_track.push_frame(frame, loop=self._loop)

                    # CRITICAL: Yield control to asyncio loop to handle WebRTC packets
                    await asyncio.sleep(0.005)
                except Exception:
                    await asyncio.sleep(0.005)

        try:
            self._loop.run_until_complete(_main())
        except Exception as e:
            logger_mp.error(f"[Teleimager] WebRTC Thread Error: {e}")
        finally:
            if self._loop:
                try:
                    self._loop.run_until_complete(self._shutdown())
                except Exception as e:
                    logger_mp.warning(f"[Teleimager] WebRTC cleanup failed: {e}")
                self._loop.close()

    async def _shutdown(self):
        for pc in list(self._pcs):
            try:
                await pc.close()
            except Exception:
                pass
        self._pcs.clear()
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass

    def send(self, data: np.ndarray):
        """Send data to the processing thread."""
        # Simple drop-frame logic if queue is full
        if not self._frame_queue.full():
            self._frame_queue.put(data)
        else:
            try:
                self._frame_queue.get_nowait()
                self._frame_queue.put(data)
            except: pass

    def stop(self):
        self._stop_event.set()
        self.join(timeout=1.0)


# ========================================================
# WebRTC Manager
# ========================================================
class WebRTC_PublisherManager:
    """Manages WebRTC_PublisherThreads."""
    _instance: Optional["WebRTC_PublisherManager"] = None
    _publisher_threads: Dict[Tuple[str, int], WebRTC_PublisherThread] = {}
    _lock = threading.Lock()
    _running = True

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "WebRTC_PublisherManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _create_publisher(self, port: int, host: str, codec_pref: str):
        t = WebRTC_PublisherThread(port, host, codec_pref)
        t.start()
        if not t.wait_for_start(timeout=10.0):  # Increase timeout to 10 seconds
             logger_mp.error(f"[Teleimager] WebRTC publisher on {host}:{port} failed to start.")
        return t

    def _get_publisher(self, port, host, codec_pref):
        key = (host, port)
        with self._lock:
            if key not in self._publisher_threads:
                self._publisher_threads[key] = self._create_publisher(port, host, codec_pref)
            return self._publisher_threads[key]

    def publish(self, data: Any, port: int, host: str = "0.0.0.0", codec_pref: str = None) -> None:
        if not self._running: return
        try:
            pub = self._get_publisher(port, host, codec_pref)
            pub.send(data)
        except Exception as e:
            logger_mp.error(f"[Teleimager] Unexpected error in publish: {e}")
            pass

    def close(self) -> None:
        self._running = False
        with self._lock:
            for key, pub in list(self._publisher_threads.items()):
                try:
                    pub.stop()
                except Exception: pass
            self._publisher_threads.clear()


# ========================================================
# UVC driver reload
# ========================================================
def reload_uvcvideo_module():
    """Reset the UVC driver so a stale device state cannot carry over.

    Worth doing on every start: after a crash or a replug libuvc may not have
    reattached the kernel driver, and the camera is then stuck half-open.

    No module parameter is passed. `debug=0` used to be, but uvcvideo has no
    such parameter (the real one is `trace`, which already defaults to 0), so
    the kernel logged "unknown parameter 'debug' ignored" on every start.

    The unload is best-effort: it fails while something still holds the device,
    and in that case re-loading is both unnecessary and harmless to skip -- but
    the load below must still run, otherwise a successful unload would leave
    the machine with no driver at all.
    """
    # Already root (the systemd service from setup_autostart.sh runs as root):
    # privilege escalation is unnecessary, and a sudoers entry may not exist.
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    try:
        # -n for non-interactive: without it, a user who has not run
        # --setup-permissions gets a password prompt on a tty, and a silent
        # hang on a tty-less start (systemd, CI). With it, the missing entry
        # fails immediately into the message below.
        subprocess.run([*sudo, "modprobe", "-r", "uvcvideo"], timeout=30)
        subprocess.run([*sudo, "modprobe", "uvcvideo"], check=True, timeout=30)
        # No sudo: settle only waits for the event queue to drain, and keeping
        # it unprivileged means the sudoers entry stays down to modprobe.
        subprocess.run(["udevadm", "settle"], timeout=30)
        logger_mp.info("[Teleimager] UVC driver reloaded.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger_mp.error(f"[Teleimager] Failed to reload driver: {e}")
        logger_mp.error('[Teleimager] Grant it once with:\n'
                        '    sudo "$(which teleimager-server)" --setup-permissions')


# ========================================================
# host permissions (one-shot, run as root)
# ========================================================
# The two backends reach the hardware through different nodes and the distro
# treats them very differently:
#   /dev/video*        GROUP="video" + a uaccess tag, so a desktop session gets
#                      an ACL for free -> V4L2 works out of the box.
#   /dev/bus/usb/*/*   MODE="0664" with no group, and 70-uaccess.rules only tags
#                      HID devices (060101), never video class (0e01xx) -> the
#                      node stays root-owned and libusb_open() returns EACCES.
# libuvc (the "uvc" backend) goes through usbfs, which is why it is the one
# that breaks.
_UDEV_RULE_PATH = "/etc/udev/rules.d/70-teleimager.rules"
# The 70- prefix matters: 73-seat-late.rules is where RUN{builtin}+="uaccess"
# applies the ACL, so the tag must be set before it. 0e01?? rather than 0e0100
# because udev uses shell globs ("?" = any single char, man 7 udev) and UVC 1.5
# devices report protocol 01. Both GROUP= and TAG+= are set on purpose: uaccess
# covers the desktop session immediately, the group is the fallback for SSH /
# headless, which have no seat.
_UDEV_RULE = ('SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", '
              'ENV{ID_USB_INTERFACES}=="*:0e01??:*", '
              'GROUP="video", MODE="0660", TAG+="uaccess"\n')
# Must be 0440 and owned by root, or sudo refuses to read the directory.
_SUDOERS_PATH = "/etc/sudoers.d/uvc_modprobe"


def _run(argv, required=True):
    """Run a host command; report failure instead of silently carrying on.

    `required=False` is for steps that are allowed to fail on a stripped-down
    image (a missing group, no modprobe) without making the whole setup look
    like it worked.
    """
    try:
        subprocess.run(argv, check=required, timeout=30)
        return True
    except FileNotFoundError:
        logger_mp.error(f"[Teleimager] {argv[0]} not found; skipping: {' '.join(argv)}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger_mp.error(f"[Teleimager] {' '.join(argv)} failed: {e}")
    return False


def setup_permissions(requested_user=None):
    """Grant a normal user access to the cameras. Run once, as root.

    Installs the udev rule above, adds the invoking user to 'video', and grants
    the passwordless modprobe that reload_uvcvideo_module() needs.
    """
    if os.geteuid() != 0:
        logger_mp.error("[Teleimager] This writes to /etc/udev and /etc/sudoers.d, "
                        "so it needs root:\n"
                        '    sudo "$(which teleimager-server)" --setup-permissions')
        return 1

    # Whoever ran this under sudo. No "ALL" fallback: that would hand the
    # passwordless modprobe to every account on the machine.
    user = requested_user or os.environ.get("SUDO_USER") or None
    if user == "root":
        user = None
    if requested_user:
        try:
            pwd.getpwnam(requested_user)
        except KeyError:
            logger_mp.error(f"[Teleimager] No such user: {requested_user}")
            return 1

    try:
        with open(_UDEV_RULE_PATH, "w") as f:
            f.write(_UDEV_RULE)
        os.chmod(_UDEV_RULE_PATH, 0o644)
        logger_mp.info(f"[Teleimager] Installed {_UDEV_RULE_PATH}")
    except OSError as e:
        logger_mp.error(f"[Teleimager] Cannot write {_UDEV_RULE_PATH}: {e}")
        return 1

    # Rules only apply to new events, so already-plugged cameras need a
    # synthetic 'change' to pick up the ACL without a replug.
    _run(["udevadm", "control", "--reload-rules"])
    _run(["udevadm", "trigger", "--subsystem-match=usb", "--action=change"])

    needs_relogin = False
    if user:
        try:
            grp.getgrnam("video")
        except KeyError:
            # uaccess still covers a desktop session; only the SSH/headless
            # fallback is lost, so this is a warning rather than a failure.
            logger_mp.warning("[Teleimager] No 'video' group on this system; "
                              "desktop sessions still work via uaccess.")
        else:
            if user in grp.getgrnam("video").gr_mem:
                logger_mp.info(f"[Teleimager] {user} is already in the 'video' group.")
            elif _run(["usermod", "-aG", "video", user]):
                needs_relogin = True

    # Root needs no grant of its own; the steps left are all per-account.
    if not user:
        logger_mp.info("[Teleimager] Running as root; the udev rule is in place. "
                       "Pass --user <name> to grant a normal account.")
        return 0

    # sudo matches the whole command line, so this must stay in sync with
    # reload_uvcvideo_module() above. Validate before installing: a malformed
    # file in /etc/sudoers.d/ locks sudo out of the machine.
    modprobe = shutil.which("modprobe") or "/usr/sbin/modprobe"
    rule = ("# Teleimager: reload the UVC driver without a password.\n"
            "%s ALL=(ALL) NOPASSWD: %s -r uvcvideo, %s uvcvideo\n"
            % (user, modprobe, modprobe))
    # tempfile puts this in /tmp rather than in the udev rules dir, where an
    # abandoned file would be parsed as a rule on every reload.
    try:
        fd, tmp = tempfile.mkstemp(prefix="teleimager-sudoers-")
    except OSError as e:
        logger_mp.error(f"[Teleimager] Cannot create a temp file: {e}")
        return 1
    try:
        with os.fdopen(fd, "w") as f:
            f.write(rule)
        check = subprocess.run(["visudo", "-c", "-f", tmp],
                               capture_output=True, text=True)
        if check.returncode != 0:
            out = (check.stdout or check.stderr or "").strip()[:500]
            logger_mp.error("[Teleimager] Generated sudoers entry is invalid, "
                            "not installing it:\n" + out)
            return 1
        shutil.copyfile(tmp, _SUDOERS_PATH)
        os.chmod(_SUDOERS_PATH, 0o440)
        logger_mp.info(f"[Teleimager] Installed {_SUDOERS_PATH}")
    except OSError as e:
        logger_mp.error(f"[Teleimager] Cannot install the sudoers entry: {e}")
        return 1
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    logger_mp.info("[Teleimager] Done.")
    logger_mp.info("[Teleimager] Desktop sessions: UVC works now, nothing to restart.")
    if needs_relogin:
        # usermod only edits /etc/group; a process keeps the group set it was
        # started with. sshd reads the group list fresh on every login, so an
        # SSH reconnect is enough -- but a terminal started from the desktop
        # inherits the already-running user manager's stale set, so that one
        # needs a logout (or reboot).
        logger_mp.info("[Teleimager] SSH: reconnect ('sshd' re-reads groups per "
                       "login).  Desktop terminal: log out and back in.")
    logger_mp.info("[Teleimager] Verify with:  teleimager-server --cf --uvc")
    return 0


# ========================================================
# camera finder and cameras
# ========================================================
class CameraFinder:
    """
    Discover connected cameras and their properties.
    """
    def __init__(self, enable_uvc=False, enable_v4l2=False, enable_gstreamer=False, enable_realsense=False):
        self.enable_uvc = enable_uvc
        self.enable_v4l2 = enable_v4l2
        self.enable_gstreamer = enable_gstreamer
        self.enable_realsense = enable_realsense
        self.report()

    def report(self):
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
        n = len(children)
        for i, (label, sub) in enumerate(children):
            last = i == n - 1
            logger_mp.info("%s%s%s", prefix, "└─ " if last else "├─ ", label)
            if sub:
                CameraFinder._print_children(sub, prefix + ("   " if last else "│  "))

    @staticmethod
    def _leaf(text):
        return (text, [])

    def _report_realsense(self, cards):
        cams = []
        for c in cards:
            sn = c.get("serial_number") or "(none)"
            fields = [self._leaf("serial_number : %s" % sn)]
            modes = c.get("modes") or []
            if modes:
                # group by stream (Depth / Color / Infrared ...), then list each mode
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

            # group by resolution, collapse identical fps lists
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
                # scan() is pure sysfs and never opens the device, so the card
                # renders either way -- without this the only symptom is a
                # missing "modes" line, which reads as "this camera has no
                # resolutions" instead of "libuvc could not open it".
                # libuvc goes through /dev/bus/usb/..., which gets no logind ACL:
                # 70-uaccess.rules only tags HID (060101), not video (0e01xx).
                if "denied" in str(e).lower():
                    fields.append(self._leaf(
                        "⚠️  modes unavailable: USB access denied, run: "
                        'sudo "$(which teleimager-server)" --setup-permissions'))
                else:
                    fields.append(self._leaf("⚠️  modes unavailable: %s" % e))

            cams.append(("📷 %s (%s)" % (name, mfr), fields))
        return ("UVCCamera (%d found)   [type: uvc]" % len(cams), cams)

    def _report_v4l2(self, cards):
        # The scan drives `v4l2-ctl`; when it is absent every node is skipped and
        # "(0 found)" would read as "no camera plugged in". Report it as a child
        # node so the reason sits next to the count instead of inside it.
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

class BaseCamera:
    # Hardware reads (V4L2 DQBUF, libuvc frame poll, GStreamer appsink pull,
    # RealSense wait_for_frames) block until the driver has a frame, so the
    # capture loop must NOT pace them again: an extra per-frame sleep freezes
    # any backlog the driver queued during a stall (the loop can never drain
    # it faster than real time). Shared-memory reads return immediately and
    # still need explicit pacing — see IsaacSimCamera.
    _capture_requires_pacing = False

    def __init__(self, cam_topic, server_cfg):
        self._ready = threading.Event()
        self._cam_topic = cam_topic
        # ---- common keys: parsed here once for every camera ----
        self._img_shape = server_cfg.get("image_shape", None) # (H, W)
        self._fps = server_cfg.get("fps", 30)
        self._enable_zmq = server_cfg.get("enable_zmq", False)
        self._zmq_port = server_cfg.get("zmq_port", None)
        if self._enable_zmq:
            self._zmq_buffer = TripleRingBuffer()
        else:
            self._zmq_buffer = None

        self._enable_webrtc = server_cfg.get("enable_webrtc", False)
        self._webrtc_port = server_cfg.get("webrtc_port", None)
        self._webrtc_codec = server_cfg.get("webrtc_codec", None)
        self._enable_bgr_output = self._enable_webrtc
        if self._enable_bgr_output:
            self._bgr_buffer = TripleRingBuffer()
        else:
            self._bgr_buffer = None

    def __str__(self):
        raise NotImplementedError

    def __repr__(self):
        return self.__str__()

    def _update_frame(self):
        """Return a jepg frame as bytes, and a bgr frame as numpy array"""
        raise NotImplementedError

    def wait_until_ready(self, timeout=None):
        """Block until the camera is ready (first frame is available) or timeout occurs."""
        return self._ready.wait(timeout=timeout)

    def enable_webrtc(self):
        return self._enable_webrtc

    def enable_zmq(self):
        return self._enable_zmq

    def get_jpeg_bytes(self):
        jpeg_bytes = self._zmq_buffer.read() if self._enable_zmq and self._zmq_buffer else None
        return jpeg_bytes

    def get_bgr_frame(self):
        bgr_numpy = self._bgr_buffer.read() if self._enable_bgr_output and self._bgr_buffer else None
        return bgr_numpy

    def enable_bgr_output(self):
        """Enable BGR for App streaming independently of WebRTC."""
        if self._bgr_buffer is None:
            self._bgr_buffer = TripleRingBuffer()
        self._enable_bgr_output = True

    def get_depth_frame(self):
        """Return a depth frame as bytes, or None if not supported.
           Before call this function, must first call get_frame() to update the latest depth data."""
        return None

    def get_zmq_port(self):
        """Return the zmq port number the camera is serving on."""
        return self._zmq_port

    def get_webrtc_port(self):
        """Return the webrtc port number the camera is serving on."""
        return self._webrtc_port

    def get_webrtc_codec(self):
        """Return the webrtc codec setting."""
        return self._webrtc_codec

    def get_fps(self):
        """Return the camera FPS setting."""
        return self._fps

    def release(self):
        """Release camera resources."""
        raise NotImplementedError

class RealSenseCamera(BaseCamera):
    def __init__(self, cam_topic, serial_number, server_cfg):
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
        if self._latest_depth is None:
            return None
        return self._latest_depth.tobytes()

    def release(self):
        if self.pipeline is not None and hasattr(self.pipeline, "stop"):
            try:
                self.pipeline.stop()
            except Exception as e:
                logger_mp.warning(f"[Teleimager] pipeline.stop() failed: {e}")
        self.pipeline = None
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @staticmethod
    def get_realsense_module():
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

            # Match RealSense by name and Intel vendor ID
            if name and "realsense" in name.lower() and (vendor_id or "").lower() in ("8086", "32902"):
                ports.append(devnode)

        return ports

    # Depth + infrared v4l2 fourccs. The color sensor node advertises none of
    # these (only YUYV/MJPG/UYVY color formats); every depth/IR node carries at
    # least one. Used to keep the RealSense RGB node available to UVC/V4L2 while
    # still routing its depth/IR nodes to the realsense driver only.
    _DEPTH_IR_FMTS = {"Z16", "GREY", "Y8", "Y8I", "Y12I", "Y16", "Y10", "RW16", "W10", "CONFIDENCE"}

    @staticmethod
    def _list_pixel_formats(video_path):
        """Fourcc set a node advertises (self-contained v4l2-ctl crawl)."""
        try:
            r = subprocess.run(["v4l2-ctl", "-d", video_path, "--list-formats"],
                               capture_output=True, text=True, timeout=5)
            out = r.stdout or ""
        except Exception:
            return set()
        return {m.group(1) for m in re.finditer(r"\[\d+\]:\s*'(\w+)", out)}

    @classmethod
    def _classify_video_nodes(cls):
        """
        Split this RealSense's /dev/video* nodes into (color, depth_ir) by each
        node's advertised pixel formats. The color (RGB) sensor node exposes
        only color formats; depth/IR nodes expose at least one depth/mono
        format. Nodes with no capture format (metadata siblings) are ignored.
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
        """RealSense depth/IR nodes only — the ones UVC/V4L2 must NOT surface."""
        return cls._classify_video_nodes()[1]

    @classmethod
    def scan(cls):
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
        """
        Color stream profiles the given RealSense exposes, via the pyrealsense2
        SDK. Returns a list of {"stream", "format", "width", "height", "fps"}
        dicts, one per (format, resolution) with its fps values collapsed.
        Only the Color (RGB) stream is reported; depth / IR / motion streams
        are handled by the realsense driver, not surfaced here.
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
        serial_number = str(server_cfg.get("serial_number")) if server_cfg.get("serial_number") else None
        # Self-contained resolution: RealSense runs its OWN scan (no CameraFinder).
        serials = [c.get("serial_number") for c in cls.scan()]
        if serial_number not in serials:
            logger_mp.error(f"[Teleimager] Cannot find RealSenseCamera for {cam_topic}")
            return None
        return cls(cam_topic, serial_number, server_cfg)

class UVCCamera(BaseCamera):
    def __init__(self, cam_topic, uid, server_cfg):
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
        for m in cap.available_modes:
            if m.width == width and m.height == height and m.fps == fps and m.format_name == "MJPG":
                return m
        raise ValueError("[UVCCamera] No matching uvc mode found")

    def _update_frame(self):
        if self.cap is not None:
            frame = self.cap.get_frame_robust() # get_frame(timeout=500)
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
        # if usbhub is plugged out, calling stop_streaming and close may hang forever.
        # try:
        #     self.cap.stop_streaming()
        # except Exception:
        #     pass
        # try:
        #     self.cap.close()
        # except Exception:
        #     pass
        # self.cap = None
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @staticmethod
    def _list_video_paths():
        base = Path("/sys/class/video4linux")
        if not base.exists():
            return []
        return [f"/dev/{name}" for name in sorted(p.name for p in base.iterdir()) if name.startswith("video")]

    @staticmethod
    def _is_like_rgb(video_path):
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
        sysfs_path = Path("/sys/class/video4linux") / Path(video_path).name / "device"
        return str(sysfs_path.resolve())

    @staticmethod
    def _get_uid_from_ppath(physical_path):
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
        """
        Walk up from a video node's interface dir to the parent USB device dir
        and read its identifier attributes straight from sysfs. Pure sysfs — no
        libuvc, so it works even when the camera cannot be opened. Deliberately
        duplicated from V4L2Camera (each camera class harvests its own ids).
        Returns serial_number / bcd_device / vid_pid, plus name / manufacturer /
        idVendor / idProduct for the discovery report.
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
        """
        Discover all UVC RGB cameras via a self-contained sysfs crawl (decoupled
        from V4L2's; the duplication is intentional). Each card's fields map to
        this class's yaml identifier keys (physical_path / serial_number /
        bcd_device / vid_pid / video_id). The uid (busnum:devnum) that libuvc
        needs to OPEN the device at runtime is likewise read from sysfs.
        RealSense-owned nodes are NOT excluded here — that cross-driver
        reconciliation is done by CameraFinder, which sees all drivers.
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
        physical_path = str(server_cfg.get("physical_path")) if server_cfg.get("physical_path") else None
        serial_number = str(server_cfg.get("serial_number")) if server_cfg.get("serial_number") else None
        _bcd = server_cfg.get("bcd_device")
        bcd_device = f"{_bcd:04d}" if type(_bcd) is int else (str(_bcd) if _bcd else None)
        vid_pid = str(server_cfg.get("vid_pid")) if server_cfg.get("vid_pid") else None

        # Self-contained resolution: UVC runs its OWN scan and maps the yaml
        # identifier to the libuvc uid needed to open the device. RealSense-owned
        # nodes are dropped (libuvc cannot open them); this cross-driver exclusion
        # is duplicated here on purpose so the driver needs no CameraFinder.
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

        # once you specify either `physical_path` or `serial_number`, the system will no longer fall back to searching by `video_id`.
        # ——— even if no camera matches the given path/serial.
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

class V4L2Camera(BaseCamera):
    def __init__(self, cam_topic, video_path, server_cfg):
        super().__init__(cam_topic, server_cfg)
        self._video_path = video_path

        # JPEG-first: default to MJPG (raw JPEG straight to ZMQ, decode lazily for
        # WebRTC); fall back to YUYV (decode to BGR, re-encode for ZMQ) only when the
        # device offers no MJPG. Captured through PyAV (libav v4l2 demuxer).
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

        # Test if the camera can read frames
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
        try:
            return len(bytes(next(self._demux))) > 0
        except StopIteration:
            return False

    def _update_frame(self):
        if self.container is None:
            return
        packet = next(self._demux)
        if self._passthrough:
            # MJPG source: raw JPEG straight to ZMQ (no re-encode, like UVCCamera);
            # decode to BGR lazily only when WebRTC needs it.
            jpeg_bytes = bytes(packet)
            if not jpeg_bytes:
                return
            if self._enable_zmq:
                self._zmq_buffer.write(jpeg_bytes)
            if self._enable_bgr_output:
                self._bgr_buffer.write(_turbojpeg.decode(jpeg_bytes))
        else:
            # YUYV source: decode to BGR (libswscale); re-encode for ZMQ.
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
        """
        Query one node's capabilities + capture modes with `v4l2-ctl`.
        Returns None when v4l2-ctl is missing or the node can't be queried.
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
        """
        Definitive test that a node actually yields images. Many /dev/video*
        nodes advertise a "Video Capture" capability but produce no frames —
        e.g. the metadata / control sibling node that a UVC camera exposes
        alongside its real node. Opened via PyAV (libav v4l2), like V4L2Camera.
        """
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

    # --- Self-contained identifier harvest -------------------------------
    # V4L2 owns its own sysfs crawl and does NOT reuse the UVC scan. The
    # values it produces (physical_path / serial_number / bcd_device /
    # vid_pid) match the UVC scan's when both drivers can see the device,
    # but the logic is independent: a camera libuvc cannot open is still
    # bindable here. (Duplication with UVCCamera is intentional — each
    # camera class stays self-sufficient.)

    @staticmethod
    def _get_ppath_from_vpath(video_path):
        sysfs_path = Path("/sys/class/video4linux") / Path(video_path).name / "device"
        return str(sysfs_path.resolve())

    @staticmethod
    def _usb_attrs_from_ppath(physical_path):
        """
        Walk up from a video node's interface dir to the parent USB device
        dir and read its identifier attributes straight from sysfs. Pure
        sysfs — works even when libuvc cannot open the camera. Returns
        {"serial_number", "bcd_device", "vid_pid"} (values may be None for
        non-USB / CSI nodes). vid_pid is "xxxx:xxxx" to match the UVC scan.
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
        """
        Harvest every /dev/video* node's stable identifiers via V4L2's own
        sysfs crawl (decoupled from the UVC scan). Cheap: pure sysfs, no
        v4l2-ctl and no cv2. Used by both scan() (report) and from_config()
        (resolution).
        """
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
        """
        Return the single capture node whose identifier `key` == `value`.
        A UVC/CSI device exposes a real capture node plus metadata/control
        siblings that share the same USB identifier, so matches are narrowed
        to nodes that actually grab a frame; a still-ambiguous match (two
        distinct cameras of the same model by vid_pid) raises.
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
        """
        Discover V4L2 capture nodes via a self-contained `v4l2-ctl` crawl.
        Unlike the UVC scan this also surfaces GMSL / MIPI-CSI nodes that carry
        no USB descriptor. Card field `video_id` maps to this class's yaml
        identifier; USB cameras can still be pinned by serial_number / bcd_device
        / vid_pid, which are resolved through the UVC scan by CameraFinder.

        A node is only reported if it (a) advertises Video Capture, (b) exposes
        at least one capture pixel format, and (c) actually grabs a frame — so
        metadata / control nodes that can't produce images are filtered out.
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
        video_id = server_cfg.get("video_id", 0)
        video_path = f"/dev/video{video_id}" if video_id is not None else None
        physical_path = str(server_cfg.get("physical_path")) if server_cfg.get("physical_path") else None
        serial_number = str(server_cfg.get("serial_number")) if server_cfg.get("serial_number") else None
        _bcd = server_cfg.get("bcd_device")
        bcd_device = f"{_bcd:04d}" if type(_bcd) is int else (str(_bcd) if _bcd else None)
        vid_pid = str(server_cfg.get("vid_pid")) if server_cfg.get("vid_pid") else None

        # Self-contained resolution: V4L2 harvests its OWN sysfs identifiers (no
        # CameraFinder). A camera libuvc cannot open is still bindable by
        # physical_path / serial_number / bcd_device / vid_pid.
        ids = cls._list_identifiers()

        if physical_path is not None:
            vpath = cls._resolve_vpath(ids, "physical_path", physical_path)
            if vpath is None:
                logger_mp.error(f"[Teleimager] Cannot find V4L2Camera for {cam_topic} with physical path {physical_path}")
                return None
            else:
                return cls(cam_topic, vpath, server_cfg)

        # once you specify either `physical_path` or `serial_number`, the system will no longer fall back to searching by `video_id`.
        # ——— even if no camera matches the given path/serial.
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

class GStreamerCamera(BaseCamera):
    def __init__(self, cam_topic, gst_pipeline, server_cfg):
        super().__init__(cam_topic, server_cfg)
        self._Gst = self.get_gstreamer_module()
        self._pipeline_str = gst_pipeline

        # single pipeline ending in an appsink; composite (e.g. binocular) inside
        # the pipeline itself with compositor if you need to merge multiple sources.
        self.pipe, self.sink = self._start_pipeline(self._pipeline_str)

        # Test if the camera can read frames
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
        # A conda PyGObject ships glib/gio typelibs but no GStreamer ones, and the
        # repo reads GI_TYPELIB_PATH only at creation -- so extend it before the import.
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
        Gst = self._Gst
        pipe = Gst.parse_launch(pipeline_str)
        # locate the appsink: prefer name=sink, otherwise the first appsink element
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
        """Pull one encoded JPEG buffer from the appsink and return its raw bytes."""
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
        # first frame may need extra time for the source to warm up (e.g. CSI nvarguscamerasrc ~2s)
        return self._pull_jpeg(self.sink, timeout_ns=5_000_000_000) is not None

    def _update_frame(self):
        jpeg_bytes = self._pull_jpeg(self.sink)
        if jpeg_bytes is None:
            raise RuntimeError

        # ZMQ gets the raw JPEG bytes directly — no re-encode (like UVCCamera).
        if self._enable_zmq:
            self._zmq_buffer.write(jpeg_bytes)

        # WebRTC needs raw BGR frames, so decode lazily only when it is enabled.
        if self._enable_bgr_output:
            self._bgr_buffer.write(_turbojpeg.decode(jpeg_bytes))

        if not self._ready.is_set():
            self._ready.set()

    def release(self):
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
        # udev symlinks that realize the same identifiers the other drivers use:
        #   physical_path -> /dev/v4l/by-path  (USB topology / physical port)
        #   serial_number -> /dev/v4l/by-id    (vendor/model/serial)
        # Both are stable across replug/reboot, unlike the raw /dev/videoN.
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
        gst_pipeline = server_cfg.get("gst_pipeline")
        if not gst_pipeline:
            logger_mp.error(f"[Teleimager] type 'gstreamer' for {cam_topic} requires a 'gst_pipeline' (must contain appsink).")
            return None
        return cls(cam_topic, gst_pipeline, server_cfg)

class R1StereoCamera(BaseCamera):
    """Receive R1's RTP H264 or JPEG eyes as one side-by-side head camera."""

    def __init__(self, cam_topic, server_cfg):
        import cv2

        super().__init__(cam_topic, server_cfg)
        self._cv2 = cv2
        img_shape = self._img_shape
        bind_host = server_cfg.get("bind_host", "0.0.0.0")
        source_host = server_cfg.get("source_host", "192.168.123.161")
        left_port = server_cfg.get("left_port", 5002)
        right_port = server_cfg.get("right_port", 5003)
        receive_timeout = server_cfg.get("receive_timeout", 2.0)
        max_pair_delay = server_cfg.get("max_pair_delay", 0.05)
        rtp_encoding = server_cfg.get("rtp_encoding", "H264")
        height, width = img_shape
        if height <= 0 or width <= 0 or width % 2:
            raise ValueError("R1 stereo image_shape requires a positive height and positive even width.")
        if receive_timeout <= 0 or not 0 < max_pair_delay < receive_timeout:
            raise ValueError("R1 stereo requires 0 < max_pair_delay < receive_timeout.")
        if rtp_encoding not in ("H264", "JPEG"):
            raise ValueError("R1 stereo rtp_encoding must be H264 or JPEG.")
        self._rtp_encoding = rtp_encoding
        self._source_host = source_host
        self._receive_timeout = receive_timeout
        self._max_pair_delay = max_pair_delay
        self._selector = selectors.DefaultSelector()
        self._sockets = []
        self._jpeg_processes = []
        self._receivers = []
        self._closed = threading.Event()
        self._last_pair_time = None
        try:
            for port in (left_port, right_port):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sockets.append(sock)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                sock.bind((bind_host, port))
                if rtp_encoding == "JPEG":
                    # fdsrc preserves UDP datagram boundaries; the parent owns
                    # binding/cleanup, including ephemeral ports used by tests.
                    # Leave payload type unspecified: R1 firmware uses 26 or 96.
                    process = subprocess.Popen(
                        ["gst-launch-1.0", "-q", "fdsrc", f"fd={sock.fileno()}",
                         "blocksize=65536", "do-timestamp=true", "!",
                         "application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG",
                         "!", "rtpjitterbuffer", "latency=20", "drop-on-latency=true",
                         "!", "rtpjpegdepay", "!", "jpegparse",
                         "!", "fdsink", "fd=1", "sync=false"],
                        pass_fds=(sock.fileno(),), stdout=subprocess.PIPE,
                    )
                    self._jpeg_processes.append(process)
                    os.set_blocking(process.stdout.fileno(), False)
                    decoder = av.CodecContext.create("mjpeg", "r")
                    decoder.thread_count = 1
                    receiver = {
                        "decoder": decoder, "pending": None, "latest": None,
                    }
                    self._receivers.append(receiver)
                    self._selector.register(process.stdout, selectors.EVENT_READ, receiver)
                    continue
                sock.setblocking(False)
                decoder = H264Decoder()
                decoder.codec.thread_count = 1
                receiver = {
                    "jitter": JitterBuffer(capacity=128, is_video=True),
                    "decoder": decoder,
                    "reformatter": VideoReformatter(),
                    "ssrc": None,
                    "pending": None,
                    "latest": None,
                    "frame_times": {},
                    "last_frame_time": None,
                }
                self._receivers.append(receiver)
                self._selector.register(sock, selectors.EVENT_READ, receiver)
        except Exception:
            self.release()
            raise
        logger_mp.info(str(self))

    def __str__(self):
        ports = [sock.getsockname()[1] for sock in self._sockets]
        return (f"[R1StereoCamera: {self._cam_topic}] RTP/{self._rtp_encoding} left/right ports={ports}, "
                f"output={self._img_shape[1]}x{self._img_shape[0]} @ {self._fps} FPS")

    def _update_frame(self):
        if self._last_pair_time is None:
            self._last_pair_time = time.monotonic()
        while not self._closed.is_set():
            remaining = self._receive_timeout - (time.monotonic() - self._last_pair_time)
            if remaining <= 0:
                self._last_pair_time = None
                raise TimeoutError(f"R1 stereo {self._cam_topic} received no fresh pair for {self._receive_timeout}s")
            try:
                events = self._selector.select(min(remaining, 0.05))
                for key, _ in events:
                    receiver = key.data
                    if self._rtp_encoding == "JPEG":
                        data = os.read(key.fileobj.fileno(), 256 * 1024)
                        if not data:
                            if self._closed.is_set():
                                return
                            raise OSError("R1 RTP/JPEG receiver exited; inspect GStreamer stderr.")
                        received_at = time.monotonic()
                        for packet in receiver["decoder"].parse(data):
                            bgr = self._cv2.imdecode(np.frombuffer(packet, dtype=np.uint8), self._cv2.IMREAD_COLOR)
                            if bgr is not None:
                                receiver["pending"] = (bgr, received_at)
                                receiver["latest"] = (bgr, received_at)
                        continue
                    # Drain queued datagrams, keeping only the latest decoded eye.
                    for _ in range(128):
                        try:
                            data, address = key.fileobj.recvfrom(65535)
                        except BlockingIOError:
                            break
                        if address[0] != self._source_host:
                            continue
                        received_at = time.monotonic()
                        try:
                            packet = RtpPacket.parse(data)
                            if packet.payload_type != 96:
                                continue
                            packet._data = h264_depayload(packet.payload)
                        except ValueError:
                            continue
                        frame_times = receiver["frame_times"]
                        last_frame_time = receiver["last_frame_time"]
                        stalled = last_frame_time is not None and received_at - last_frame_time > max(0.25, 3.0 / self._fps)
                        if receiver["ssrc"] != packet.ssrc or stalled:
                            receiver["ssrc"] = packet.ssrc
                            receiver["jitter"] = JitterBuffer(capacity=128, is_video=True)
                            receiver["decoder"] = H264Decoder()
                            receiver["decoder"].codec.thread_count = 1
                            receiver["pending"] = None
                            receiver["latest"] = None
                            receiver["frame_times"].clear()
                            receiver["last_frame_time"] = received_at
                        # JitterBuffer emits a frame when the next timestamp arrives;
                        # retain the original receipt time across gaps and reordering.
                        frame_times.setdefault(packet.timestamp, received_at)
                        if len(frame_times) > 128:
                            del frame_times[next(iter(frame_times))]
                        lost_frame, encoded = receiver["jitter"].add(packet)
                        if lost_frame:
                            receiver["decoder"] = H264Decoder()
                            receiver["decoder"].codec.thread_count = 1
                            receiver["pending"] = None
                        if encoded is not None:
                            receiver["last_frame_time"] = received_at
                            frame_received_at = frame_times.pop(encoded.timestamp, None)
                            if frame_received_at is None:
                                continue
                            for frame in receiver["decoder"].decode(encoded):
                                bgr = receiver["reformatter"].reformat(frame, format="bgr24").to_ndarray()
                                receiver["pending"] = (bgr, frame_received_at)
                                receiver["latest"] = (bgr, frame_received_at)
            except (OSError, ValueError):
                if self._closed.is_set():
                    return
                raise

            left, right = (receiver["pending"] for receiver in self._receivers)
            if left is None or right is None:
                continue
            # Independent RTP clocks have different origins. This bounds reception
            # skew for preview, rather than claiming synchronized exposure times.
            if abs(left[1] - right[1]) > self._max_pair_delay:
                self._receivers[0 if left[1] < right[1] else 1]["pending"] = None
                continue
            height, width = self._img_shape
            eye_width = width // 2
            output = np.zeros((height, width, 3), dtype=np.uint8)
            for eye, (bgr, _) in enumerate((left, right)):
                scale = min(eye_width / bgr.shape[1], height / bgr.shape[0])
                resized_width = max(1, round(bgr.shape[1] * scale))
                resized_height = max(1, round(bgr.shape[0] * scale))
                resized = self._cv2.resize(bgr, (resized_width, resized_height))
                x = eye * eye_width + (eye_width - resized_width) // 2
                y = (height - resized_height) // 2
                output[y:y + resized_height, x:x + resized_width] = resized
            if self._enable_zmq:
                ok, buf = self._cv2.imencode(".jpg", output)
                if ok:
                    self._zmq_buffer.write(buf.tobytes())
            if self._enable_bgr_output:
                self._bgr_buffer.write(output)
            for receiver in self._receivers:
                receiver["pending"] = None
            self._last_pair_time = time.monotonic()
            self._ready.set()
            return

    def get_eye_frames(self):
        """Independent fresh eyes for App preview; paired outputs remain unchanged."""
        now = time.monotonic()
        frames = []
        for receiver in self._receivers:
            latest = receiver["latest"]
            frames.append(latest[0] if latest is not None and now - latest[1] < self._receive_timeout else None)
        return tuple(frames)

    def release(self):
        self._closed.set()
        for process in self._jpeg_processes:
            if process.poll() is None:
                process.terminate()
        for process in self._jpeg_processes:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        for sock in self._sockets:
            sock.close()
        self._selector.close()
        logger_mp.info(f"[R1StereoCamera] Released {self._cam_topic}")

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        return cls(cam_topic, server_cfg)


class IsaacSimCamera(BaseCamera):
    # Shared-memory reads return immediately, so they must be paced to the
    # configured FPS (unlike the blocking hardware backends).
    _capture_requires_pacing = True

    def __init__(self, cam_topic, server_cfg, image_source="head"):
        """
        IsaacSim camera that reads from shared memory.

        Args:
            cam_topic: camera topic name
            server_cfg: this camera's config block (common keys parsed by BaseCamera)
            image_source: which image to read from shared memory ("head", "left", "right")
        """
        super().__init__(cam_topic, server_cfg)
        from tools.shared_memory_utils import MultiImageReader # https://github.com/unitreerobotics/unitree_sim_isaaclab/tree/main/tools
        self.multi_image_reader = MultiImageReader()
        self._image_source = image_source  # "head", "left", or "right"
        self._binocular = server_cfg.get("binocular", False)  # if True and image_source=="head", concatenate left+right
        # For IsaacSim cameras, set ready immediately since the camera object is initialized
        # and will wait for shared memory data in _update_frame
        self._ready.set()
        logger_mp.info(str(self))

    def __str__(self):
        zmq = f"zmq=on (port={self._zmq_port})" if self._enable_zmq else "zmq=off"
        webrtc = f"webrtc=on (port={self._webrtc_port})" if self._enable_webrtc else "webrtc=off"
        extra = f"source={self._image_source!r}, {'binocular' if self._binocular else 'monocular'}"
        return f" 📷 Camera {self._cam_topic!r:<22} isaacsim, {self._img_shape[0]}x{self._img_shape[1]}@{self._fps}fps, {extra}, {zmq}, {webrtc}"

    def _update_frame(self):
        # Get the image data based on source and binocular settings
        frame_data = None
        if self._binocular:
            # For binocular cameras: concatenate left + right images
            left_img = self.multi_image_reader.read_single_image('left')
            right_img = self.multi_image_reader.read_single_image('right')
            logger_mp.debug(f"[Teleimager] {self._cam_topic} - left: {left_img is not None}, right: {right_img is not None}")

            if left_img is not None and right_img is not None:
                frame_data = np.hstack([left_img, right_img])
                logger_mp.debug(f"[Teleimager] {self._cam_topic} - concatenated binocular frame: {frame_data.shape}")
        else:
            # For monocular cameras: use the specified source directly
            frame_data = self.multi_image_reader.read_single_image(self._image_source)
            if frame_data is None:
                logger_mp.debug(f"[Teleimager] {self._cam_topic} - no data for source '{self._image_source}'")

        # Publish the frame data only if we have valid data
        if frame_data is not None:
            # For ZMQ: encode to JPEG bytes
            if self._enable_zmq:
                self._zmq_buffer.write(_turbojpeg.encode(frame_data))

            # For WebRTC: use BGR frames directly
            if self._enable_bgr_output:
                self._bgr_buffer.write(frame_data)

            if not self._ready.is_set():
                self._ready.set()
        else:
            logger_mp.debug(f"[Teleimager] No data available for {self._cam_topic}, frame_data is None")
        # If no data is available, just return silently and wait for next frame

    def release(self):
        if hasattr(self, 'multi_image_reader') and self.multi_image_reader is not None:
            self.multi_image_reader.close()
        self.multi_image_reader = None
        logger_mp.info(f"[Teleimager] Released {self._cam_topic}")

    @classmethod
    def scan(cls):
        """IsaacSim cameras are virtual (shared-memory / topic-convention based);
        there is no physical device to discover."""
        return []

    @classmethod
    def from_config(cls, cam_topic, server_cfg):
        # Check if binocular mode is enabled
        binocular = server_cfg.get("binocular", False)

        # For IsaacSim cameras, determine image source based on camera topic and binocular setting
        if binocular:
            # Binocular cameras (like head) need to read left+right and concatenate
            image_source = "head"  # Special marker for binocular
        else:
            # Monocular cameras read their specific source
            if "left" in cam_topic.lower():
                image_source = "left"
            elif "right" in cam_topic.lower():
                image_source = "right"
            else:
                image_source = "head"  # fallback

        return cls(cam_topic, server_cfg, image_source=image_source)
# ========================================================
# teleimage server
# ========================================================
class TeleImageServer:
    def __init__(self, server_config, enable_isaacsim=False):
        _apply_webrtc_config(server_config)
        self._server_config = server_config
        self._enable_isaacsim = enable_isaacsim
        self._stop_event = threading.Event()
        self._cameras: dict[str, BaseCamera] = {}

        self._app_stream_config = (server_config or {}).get("app_stream", {})
        self._app_stream = None
        self._app_stream_topics = ()
        self._app_stream_layout = self._app_stream_config.get("layout", "default")
        self._camera_frame_times = {}
        if self._app_stream_config.get("enabled", False):
            from .h264_tcp_stream import H264TcpStreamServer, DEFAULT_CAMERA_NAME

            self._app_stream_topics = tuple(
                self._app_stream_config.get(
                    "camera_topics",
                    ("head_camera", "left_wrist_camera", "right_wrist_camera"),
                )
            )
            if len(self._app_stream_topics) not in (1, 3):
                raise ValueError(
                    "app_stream.camera_topics must contain one camera or "
                    "head, left wrist, and right wrist cameras."
                )
            if self._app_stream_layout not in ("default", "stereo_quad"):
                raise ValueError(f"Unknown App stream layout: {self._app_stream_layout}")
            if self._app_stream_layout == "stereo_quad" and len(self._app_stream_topics) != 3:
                raise ValueError("stereo_quad camera_topics must contain head, left wrist, and right wrist.")
            self._app_stream = H264TcpStreamServer(
                control_host=self._app_stream_config.get("control_host", "0.0.0.0"),
                control_port=self._app_stream_config.get("control_port", 13579),
                camera_name=self._app_stream_config.get(
                    "camera_name",
                    DEFAULT_CAMERA_NAME,
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
        self._publisher_threads = []  # keep references for graceful join

        try:
            # Load cameras from self._server_config["camera"]
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
                    elif cam_type == "r1_stereo":
                        self._cameras[cam_topic] = R1StereoCamera.from_config(cam_topic, server_cfg)
                    elif cam_type == "isaacsim":
                        self._cameras[cam_topic] = IsaacSimCamera.from_config(cam_topic, server_cfg)
                    else:
                        logger_mp.error(f"[Teleimager] Unknown camera type {cam_type} for {cam_topic}, skipping...")
                        continue
                except Exception as exc:
                    if self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics:
                        raise
                    self._cameras[cam_topic] = None
                    logger_mp.warning(f"[App Stream] {cam_topic} initialization failed: {exc}; using nocarema tile.")

            if (
                self._server_config["camera"].get("head_camera", {}).get("enable_zmq", False)
                and self._cameras.get("head_camera") is None
            ):
                raise RuntimeError("Required ZMQ head_camera failed to initialize.")

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
        try:
            interval = 1.0 / camera.get_fps() if camera._capture_requires_pacing else 0.0
            next_frame_time = time.monotonic()
            while not self._stop_event.is_set():
                try:
                    camera._update_frame()
                    self._camera_frame_times[cam_topic] = time.monotonic()
                except Exception as e:
                    logger_mp.error(f"[Image Server] Error updating frame for {cam_topic} camera: {e}")
                    if self._app_stream_layout == "stereo_quad" and cam_topic in self._app_stream_topics:
                        self._camera_frame_times.pop(cam_topic, None)
                        if isinstance(camera, R1StereoCamera) and isinstance(e, TimeoutError):
                            continue
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
            if self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics:
                self._stop_event.set()

    def _zmq_pub(self, cam_topic: str, camera: BaseCamera):
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
            if self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics:
                self._stop_event.set()

    def _webrtc_pub(self, cam_topic: str, camera: BaseCamera):
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
            if self._app_stream_layout != "stereo_quad" or cam_topic not in self._app_stream_topics:
                self._stop_event.set()

    def _app_stream_pub(self):
        from .h264_tcp_stream import DEFAULT_WIDTH, DEFAULT_HEIGHT, compose_camera_output

        width = int(self._app_stream_config.get("width", DEFAULT_WIDTH))
        height = int(self._app_stream_config.get("height", DEFAULT_HEIGHT))

        while not self._stop_event.is_set():
            if not self._app_stream.stream_requested:
                self._stop_event.wait(0.1)
                continue
            if not self._app_stream.frame_due():
                self._stop_event.wait(0.005)
                continue

            frames = []
            for index, camera_topic in enumerate(self._app_stream_topics):
                camera = self._cameras.get(camera_topic)
                if self._app_stream_layout == "stereo_quad" and index == 0 and isinstance(camera, R1StereoCamera):
                    frames.extend(camera.get_eye_frames())
                    continue
                frame = camera.get_bgr_frame() if camera is not None else None
                if self._app_stream_layout == "stereo_quad":
                    if time.monotonic() - self._camera_frame_times.get(camera_topic, float("-inf")) >= 2.0:
                        frame = None
                    if index == 0:
                        frames.extend((None, None) if frame is None else np.hsplit(frame, 2))
                        continue
                frames.append(frame)
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

    # --------------------------------------------------------
    # public api
    # --------------------------------------------------------
    def start(self):
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
            time.sleep(2.0)  # wait a bit for IsaacSim shared memory to be ready

        for camera_topic, camera in self._cameras.items():
            if self._app_stream_layout == "stereo_quad" and camera_topic in self._app_stream_topics:
                continue
            # Use longer timeout for IsaacSim cameras since they need to wait for shared memory data
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
        self._stop_event.wait()
        self._clean_up()

    def stop(self):
        self._stop_event.set()

# ========================================================
# utility functions
# ========================================================
def signal_handler(server, signum, frame):
    logger_mp.info(f"[Teleimager] Received signal {signum}, initiating graceful shutdown...")
    server.stop()

def set_performance_mode(cores=[0, 1, 2]):
    import psutil
    try:
        p = psutil.Process(os.getpid())

        # Set CPU affinity for the process and all its threads
        p.cpu_affinity(cores)
        logger_mp.info(f"[Teleimager] CPU Affinity locked to: {cores}")

    except psutil.AccessDenied:
        logger_mp.warning("[Teleimager] Access Denied: Run as sudo for full optimization")
    except Exception as e:
        logger_mp.error(f"[Teleimager] Error: {e}")

def run_isaacsim_server():
    # Load config file, start teleimage server
    server_config = load_server_config()
    # start teleimage server
    server = TeleImageServer(server_config, enable_realsense=False, enable_isaacsim=True)
    server.start()
    return server

def main():
    # ANSI colors for --help, only when stdout is a real terminal (skipped when piped/redirected).
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
        f"  {head('Step 2')} — Use the Camera Finder Report from Step 1 to fill in the config. Edit this file:\n"
        f"      {flag('~/.config/teleimager/teleimager_server.yaml')}\n"
        "    (the default, auto-created on first run; or use your own path via --config / $TELEIMAGER_CONFIG)\n"
        "\n"
        f"  {head('Step 3')} — Start the server:\n"
        f"      {cmd('teleimager-server')}\n"
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
    parser.add_argument('--config', default=None, metavar='PATH', help='Path to a server yaml config (overrides $TELEIMAGER_CONFIG and the default user config dir).')
    parser.add_argument('--cert', default=None, metavar='PATH', help='Path to the TLS certificate (overrides the default ~/.config/teleimager/cert.pem).')
    parser.add_argument('--key', default=None, metavar='PATH', help='Path to the TLS private key (overrides the default ~/.config/teleimager/key.pem).')
    args = parser.parse_args()

    # One-shot host setup; touches only /etc, so it runs before anything that
    # reads the server config or pins CPU affinity.
    if args.setup_permissions:
        sys.exit(setup_permissions(args.user))

    global CERT_PEM_PATH, KEY_PEM_PATH
    # --cert / --key override the defaults alongside the server yaml.
    if args.cert:
        CERT_PEM_PATH = Path(args.cert).expanduser().resolve()
    if args.key:
        KEY_PEM_PATH = Path(args.key).expanduser().resolve()

    if args.affinity:
        set_performance_mode(cores=[0, 1, 2])

    server_config = load_server_config(args.config)

    # if enable camera finder mode, just print cameras info and exit
    if args.cf:
        CameraFinder(enable_uvc=args.uvc,
                     enable_v4l2=args.v4l2,
                     enable_gstreamer=args.gst,
                     enable_realsense=args.rs)
        sys.exit(0)

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

    # start teleimage server
    server = TeleImageServer(server_config, enable_isaacsim=args.isaacsim)
    server.start()

    # graceful shutdown handling
    signal.signal(signal.SIGINT, functools.partial(signal_handler, server))
    signal.signal(signal.SIGTERM, functools.partial(signal_handler, server))

    logger_mp.info("[Teleimager] Running... Press Ctrl+C to exit.")
    server.wait_until_stopped()

    # usbhub plugout may cause block process exit, no better solution for now
    # time.sleep(0.5)
    # os.killpg(os.getpgrp(), 9)

if __name__ == "__main__":
    main()
