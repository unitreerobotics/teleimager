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
import argparse
import glob
from turbojpeg import TurboJPEG
import numpy as np
import av
# uvc will be imported when needed
import yaml
import time
import threading
import signal
import functools
import re
import subprocess
import platform
from .image_client import TripleRingBuffer, ZMQ_PublisherManager, ZMQ_Responser
# webrtc dependencies
import asyncio
import json
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.rtcrtpsender import RTCRtpSender
from aiortc.contrib.media import MediaRelay
from aiortc.codecs import h264
import ssl
from pathlib import Path
import queue
import fractions
from typing import Dict, Optional, Tuple, Any

# Shared JPEG codec (libturbojpeg): encode(BGR)->jpeg bytes, decode(jpeg)->BGR.
_turbojpeg = TurboJPEG()

# ========================================================
# cam_config_server.yaml path
# ========================================================
CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "cam_config_server.yaml")

# ========================================================
# certificate and key paths
# ========================================================
module_dir = Path(__file__).resolve().parent.parent.parent
default_cert = module_dir / "cert.pem"
default_key = module_dir / "key.pem"
env_cert = os.getenv("XR_TELEOP_CERT")
env_key = os.getenv("XR_TELEOP_KEY")
user_config_dir = Path.home() / ".config" / "xr_teleoperate"
user_cert = user_config_dir / "cert.pem"
user_key = user_config_dir / "key.pem"
CERT_PEM_PATH = Path(env_cert or (user_cert if user_cert.exists() else default_cert))
KEY_PEM_PATH = Path(env_key or (user_key if user_key.exists() else default_key))
CERT_PEM_PATH = CERT_PEM_PATH.resolve()
KEY_PEM_PATH = KEY_PEM_PATH.resolve()

LOGO_SVG_PATH = Path(__file__).resolve().with_name("unitree_logo.svg")
with open(LOGO_SVG_PATH, "r", encoding="utf-8") as f:
    UNITREE_LOGO_SVG = f.read()

# ========================================================
# WebRTC global encoder config (bitrate caps + GOP).
# aiortc's bitrate constants are module-level, so this is global by design.
# ========================================================
_GOP_LENGTH = 60  # frames between keyframes; overridden by yaml if present

def _apply_webrtc_config(cam_config):
    global _GOP_LENGTH
    cfg = (cam_config or {}).get("webrtc", {})
    bitrate = cfg.get("bitrate", {})
    from aiortc.codecs import vpx
    for key, attr in (("min", "MIN_BITRATE"), ("default", "DEFAULT_BITRATE"), ("max", "MAX_BITRATE")):
        if key in bitrate and int(bitrate[key]) >= 100_000:
            setattr(h264, attr, int(bitrate[key]))
            setattr(vpx, attr, int(bitrate[key]))
    if "gop_length" in cfg and int(cfg["gop_length"]) > 0:
        _GOP_LENGTH = int(cfg["gop_length"])
    logger_mp.info(f"[WebRTC] bitrate min/default/max={h264.MIN_BITRATE}/{h264.DEFAULT_BITRATE}/{h264.MAX_BITRATE}, gop={_GOP_LENGTH}")

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
            logger_mp.error(f"[H264 Patch] Initialization failed: {e}")
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
        logger_mp.warning(f"[H264 Patch] Encode error: {e}")

h264.H264Encoder._encode_frame = jetson_software_encode_frame

# ========================================================
# Embed HTML and JS directly
# ========================================================
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
            logger_mp.debug(f"Conversion failed: {e}")
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
            logger_mp.warning(f"[WebRTC] Rejected malformed offer (missing sdp/type) for port:{self._port}")
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
                        logger_mp.info(f"[WebRTC] Preferred H264 for port:{self._port}")
                    else:
                        logger_mp.warning(f"[WebRTC] H264 preferred but not found, using auto-negotiation for port:{self._port}")
                        
                elif pref == "vp8":
                    vp8_codecs = [c for c in capabilities.codecs if c.mimeType == "video/VP8"]
                    if vp8_codecs:
                        transceiver.setCodecPreferences(vp8_codecs)
                        logger_mp.info(f"[WebRTC] Preferred VP8 for port:{self._port}")
                    else:
                        logger_mp.warning(f"[WebRTC] VP8 preferred but not found, using auto-negotiation for port:{self._port}")
                
                else:
                    h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
                    if h264_codecs:
                        transceiver.setCodecPreferences(h264_codecs)
                        logger_mp.info(f"[WebRTC] Preferred codec '{pref}' not found, falling back to H264 for port:{self._port}")
                    else:
                        logger_mp.warning(f"[WebRTC] Preferred codec '{pref}' not found, using auto-negotiation for port:{self._port}")
                    
            except Exception as e:
                logger_mp.error(f"Relay subscription failed: {e}")

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
            logger_mp.warning(f"[WebRTC] Negotiation failed for port:{self._port}: {e}")
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
            logger_mp.error(f"WebRTC Thread Error: {e}")
        finally:
            if self._loop: self._loop.close()

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
             raise ConnectionError("Publisher failed to start (Timeout)")
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
            logger_mp.error(f"Unexpected error in publish: {e}")
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
    try:
        subprocess.run("sudo modprobe -r uvcvideo", shell=True, check=True)
        subprocess.run("sudo modprobe uvcvideo debug=0", shell=True, check=True)
        subprocess.run("sudo udevadm settle", shell=True, check=True)
        logger_mp.info("UVC driver reloaded successfully.")
    except subprocess.CalledProcessError as e:
        logger_mp.error(f"Failed to reload driver: {e}")

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
        if not any([enable_uvc, enable_v4l2, enable_gstreamer, enable_realsense]):
            logger_mp.warning("🖼️ No camera backends enabled.")

    def report(self):
        if self.enable_uvc or self.enable_v4l2 or self.enable_gstreamer:
            reload_uvcvideo_module()

        rs_cards = RealSenseCamera.scan() if self.enable_realsense else []
        rs_video_paths = set(RealSenseCamera._list_video_paths())
        rs_depth_ir_paths = set(RealSenseCamera._list_depth_ir_paths())

        uvc_cards = [c for c in UVCCamera.scan() if c["video_path"] not in rs_video_paths] if self.enable_uvc else []
        v4l2_cards = [c for c in V4L2Camera.scan() if c["video_path"] not in rs_depth_ir_paths] if self.enable_v4l2 else []
        gst_cards = [c for c in GStreamerCamera.scan() if c["video_path"] not in rs_depth_ir_paths] if self.enable_gstreamer else []

        branches = [
            self._report_realsense(rs_cards),
            self._report_uvc(uvc_cards),
            self._report_v4l2(v4l2_cards),
            self._report_gstreamer(gst_cards),
        ]
        branches = [b for b in branches if b is not None]

        logger_mp.info("🖼️ Camera Finder Report")
        self._print_children(branches, "🖼️ ")

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
        if not cards:
            return None
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
            cams.append(("RealSense", fields))
        return ("RealSenseCamera (%d found)   [type: realsense]" % len(cams), cams)

    def _report_uvc(self, cards):
        if not cards:
            return None
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
            except Exception:
                pass

            cams.append(("%s (%s)" % (name, mfr), fields))
        return ("UVCCamera (%d found)   [type: uvc]" % len(cams), cams)

    def _report_v4l2(self, cards):
        if not cards:
            return None
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
            cams.append(("%s (%s)" % (name, mfr), fields))
        return ("V4L2Camera (%d found)   [type: v4l2]" % len(cams), cams)

    def _report_gstreamer(self, cards):
        if not cards:
            return None
        cams = []
        for c in cards:
            cams.append((c.get("name"), [
                self._leaf("type: gstreamer"),
                self._leaf('gst_pipeline: "%s"' % c.get("gst_pipeline")),
            ]))
        return ("GStreamerCamera (%d found)   [type: gstreamer]" % len(cams), cams)

class BaseCamera:
    def __init__(self, cam_topic, img_shape, fps, 
                 enable_zmq=True, zmq_port=55555, enable_webrtc=False, webrtc_port=60001, webrtc_codec=None):
        self._ready = threading.Event()
        self._cam_topic = cam_topic
        self._img_shape = img_shape # (H, W)
        self._fps = fps
        self._enable_zmq = enable_zmq
        self._zmq_port = zmq_port
        if self._enable_zmq:
            self._zmq_buffer = TripleRingBuffer()
        else:
            self._zmq_buffer = None

        self._enable_webrtc = enable_webrtc
        self._webrtc_port = webrtc_port
        self._webrtc_codec = webrtc_codec
        if self._enable_webrtc:
            self._webrtc_buffer = TripleRingBuffer()
        else:
            self._webrtc_buffer = None

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
        bgr_numpy = self._webrtc_buffer.read() if self._enable_webrtc and self._webrtc_buffer else None
        return bgr_numpy

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
    def __init__(self, cam_topic, serial_number, img_shape, fps, 
                 enable_zmq=True, zmq_port = 55555, enable_webrtc=False, webrtc_port=60001, webrtc_codec=None, enable_depth=False):
        rs = self.get_realsense_module()
        super().__init__(cam_topic, img_shape, fps, enable_zmq, zmq_port, enable_webrtc, webrtc_port, webrtc_codec)
        self._serial_number = serial_number
        self._enable_depth = enable_depth
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
                logger_mp.error('[RealSenseCamera] pipe_profile.get_device() is None .')
            if self._enable_depth:
                assert self._device is not None
                depth_sensor = self._device.first_depth_sensor()
                self.g_depth_scale = depth_sensor.get_depth_scale()

            self.intrinsics = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
            logger_mp.info(str(self))
        except Exception as e:
            if self.pipeline:
                try:
                    self.pipeline.stop()
                except:
                    pass
            raise RuntimeError(f"[RealSenseCamera] Failed to initialize RealSense camera {self._serial_number}: {e}")

    def __str__(self):
        return (
            f"[RealSenseCamera: {self._cam_topic}] initialized with "
            f"{self._img_shape[0]}x{self._img_shape[1]} @ {self._fps} FPS.\n"
            f"ZMQ: {'enabled, zmq_port=' + str(self._zmq_port) if self._enable_zmq else 'disabled'}; "
            f"WebRTC: {'enabled, webrtc_port=' + str(self._webrtc_port) if self._enable_webrtc else 'disabled'}"
        )

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

        if self._enable_webrtc:
            self._webrtc_buffer.write(bgr_numpy)

        if self._enable_zmq:
            self._zmq_buffer.write(_turbojpeg.encode(bgr_numpy))

        if not self._ready.is_set():
            self._ready.set()

    def get_depth_frame(self):
        if self._latest_depth is None:
            return None
        return self._latest_depth.tobytes()

    def release(self):
        try:
            if hasattr(self.pipeline, "stop") and getattr(self.pipeline, "_running", False):
                try:
                    self.pipeline.stop()
                except Exception as e:
                    logger_mp.warning(f"[RealSenseCamera] pipeline.stop() failed: {e}")
        except Exception:
            pass
        self.pipeline = None
        logger_mp.info(f"[RealSenseCamera] Released {self._cam_topic}")

    @staticmethod
    def get_realsense_module():
        try:
            import pyrealsense2 as rs
            return rs
        except ImportError:
            arch = platform.machine()
            system = platform.system()
            print(f"[RealSense] Platform: {system} / {arch}")

            if system == "Linux" and arch.startswith("aarch64"):
                msg = (
                    "[RealSense] pyrealsense2 not installed. please build from source:\n"
                    "    cd ~\n"
                    "    git clone https://github.com/IntelRealSense/librealsense.git\n"
                    "    cd librealsense\n"
                    "    git checkout v2.50.0\n"
                    "    mkdir build && cd build\n"
                    "    cmake .. -DBUILD_PYTHON_BINDINGS=ON -DPYTHON_EXECUTABLE=$(which python3)\n"
                    "    make -j$(nproc)\n"
                    "    sudo make install\n"
                )
            else:
                msg = (
                    "[RealSense] pyrealsense2 not installed. You can try:\n"
                    "    pip install pyrealsense2\n"
                )
            raise RuntimeError(msg)

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
            d = os.path.realpath(os.path.join(video_sysdir, "device"))
            for _ in range(10):
                if d is None or d == "/" or not os.path.isdir(d):
                    break
                id_vendor = _read_text(os.path.join(d, "idVendor"))
                id_product = _read_text(os.path.join(d, "idProduct"))
                if id_vendor and id_product:
                    return d
                d_next = os.path.dirname(d)
                if d_next == d:
                    break
                d = d_next
            return None

        ports = []
        for devnode in sorted(glob.glob("/dev/video*")):
            sysdir = f"/sys/class/video4linux/{os.path.basename(devnode)}"
            name = _read_text(os.path.join(sysdir, "name"))
            usb_dir = _parent_usb_device_sysdir(sysdir)
            vendor_id = _read_text(os.path.join(usb_dir, "idVendor")) if usb_dir else None

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
    def from_config(cls, cam_topic, cam_cfg, base_kwargs, enable_realsense):
        serial_number = str(cam_cfg.get("serial_number")) if cam_cfg.get("serial_number") else None
        if not enable_realsense:
            logger_mp.error(f"[Image Server] Please start image server with the '--rs' flag to support Realsense {cam_topic}.")
            return None
        # Self-contained resolution: RealSense runs its OWN scan (no CameraFinder).
        serials = [c.get("serial_number") for c in cls.scan()]
        if serial_number not in serials:
            logger_mp.error(f"[Image Server] Cannot find RealSenseCamera for {cam_topic}")
            return None
        return cls(cam_topic, serial_number, **base_kwargs)

class UVCCamera(BaseCamera):
    def __init__(self, cam_topic, uid, img_shape, fps, 
                 enable_zmq=True, zmq_port=55555, enable_webrtc=False, webrtc_port=60001, webrtc_codec=None):
        super().__init__(cam_topic, img_shape, fps, enable_zmq, zmq_port, enable_webrtc, webrtc_port, webrtc_codec)
        uvc = self.get_uvc_module()
        self.uid = uid
        self.cap = None
        try:
            self.cap = uvc.Capture(self.uid)
        except Exception as e:
            self.cap = None
            raise RuntimeError(f"[UVCCamera] Failed to open camera {self._cam_topic}: {e}")

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
                "[UVC] pupil-labs-uvc (import name 'uvc') not installed.  You can try:\n"
                "    sudo apt install -y libusb-1.0-0-dev libturbojpeg-dev\n"
                "    pip install pupil-labs-uvc\n"
            )
            raise RuntimeError(msg)

    def __str__(self):
        return (
            f"[UVCCamera: {self._cam_topic}] initialized with "
            f"{self._img_shape[0]}x{self._img_shape[1]} @ {self._fps} FPS, MJPG.\n"
            f"ZMQ: {'enabled, zmq port=' + str(self._zmq_port) if self._enable_zmq else 'disabled'}; "
            f"WebRTC: {'enabled, webrtc port=' + str(self._webrtc_port) if self._enable_webrtc else 'disabled'}"
        )

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

                if self._enable_webrtc:
                    if frame.bgr is not None:
                        self._webrtc_buffer.write(frame.bgr)

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
        logger_mp.info(f"[UVCCamera] Released {self._cam_topic}")

    @staticmethod
    def _list_video_paths():
        base = "/sys/class/video4linux/"
        if not os.path.exists(base):
            return []
        return [f"/dev/{x}" for x in sorted(os.listdir(base)) if x.startswith("video")]

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
        sysfs_path = f"/sys/class/video4linux/{os.path.basename(video_path)}/device"
        return os.path.realpath(sysfs_path)

    @staticmethod
    def _get_uid_from_ppath(physical_path):
        def read_file(path):
            return open(path).read().strip() if os.path.exists(path) else None

        busnum_file = os.path.join(physical_path, "busnum")
        devnum_file = os.path.join(physical_path, "devnum")
        if not (os.path.exists(busnum_file) and os.path.exists(devnum_file)):
            parent = os.path.dirname(physical_path)
            busnum_file = os.path.join(parent, "busnum")
            devnum_file = os.path.join(parent, "devnum")
        if os.path.exists(busnum_file) and os.path.exists(devnum_file):
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

        d = physical_path
        for _ in range(10):
            if not d or d == "/" or not os.path.isdir(d):
                break
            vid = _read(os.path.join(d, "idVendor"))
            pid = _read(os.path.join(d, "idProduct"))
            if vid and pid:
                return {
                    "serial_number": _read(os.path.join(d, "serial")),
                    "bcd_device": _read(os.path.join(d, "bcdDevice")),
                    "vid_pid": f"{vid}:{pid}",
                    "name": _read(os.path.join(d, "product")),
                    "manufacturer": _read(os.path.join(d, "manufacturer")),
                    "idVendor": vid,
                    "idProduct": pid,
                }
            nxt = os.path.dirname(d)
            if nxt == d:
                break
            d = nxt
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
    def from_config(cls, cam_topic, cam_cfg, base_kwargs):
        physical_path = str(cam_cfg.get("physical_path")) if cam_cfg.get("physical_path") else None
        serial_number = str(cam_cfg.get("serial_number")) if cam_cfg.get("serial_number") else None
        _bcd = cam_cfg.get("bcd_device")
        bcd_device = f"{_bcd:04d}" if type(_bcd) is int else (str(_bcd) if _bcd else None)
        vid_pid = str(cam_cfg.get("vid_pid")) if cam_cfg.get("vid_pid") else None

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
                logger_mp.error(f"[Image Server] Cannot find UVCCamera for {cam_topic} with physical path {physical_path}")
            else:
                return cls(cam_topic, uid, **base_kwargs)

        # once you specify either `physical_path` or `serial_number`, the system will no longer fall back to searching by `video_id`.
        # ——— even if no camera matches the given path/serial.
        if serial_number is not None:
            uid = _uid("serial_number", serial_number, unique=True)
            if uid is None:
                logger_mp.error(f"[Image Server] Cannot find UVCCamera for {cam_topic} with serial number {serial_number}")
                return None
            return cls(cam_topic, uid, **base_kwargs)

        if bcd_device is not None:
            uid = _uid("bcd_device", bcd_device, unique=True)
            if uid is None:
                logger_mp.error(f"[Image Server] Cannot find UVCCamera for {cam_topic} with bcd_device {bcd_device}")
                return None
            return cls(cam_topic, uid, **base_kwargs)

        if vid_pid is not None:
            uid = _uid("vid_pid", vid_pid, unique=True)
            if uid is None:
                logger_mp.error(f"[Image Server] Cannot find UVCCamera for {cam_topic} with vid_pid {vid_pid}")
                return None
            return cls(cam_topic, uid, **base_kwargs)

        return None

class V4L2Camera(BaseCamera):
    def __init__(self, cam_topic, video_path, img_shape, fps,
                 enable_zmq=True, zmq_port=55555, enable_webrtc=False, webrtc_port=60001, webrtc_codec=None):
        super().__init__(cam_topic, img_shape, fps, enable_zmq, zmq_port, enable_webrtc, webrtc_port, webrtc_codec)
        self._video_path = video_path

        # JPEG-first: default to MJPG (raw JPEG straight to ZMQ, decode lazily for
        # WebRTC); fall back to YUYV (decode to BGR, re-encode for ZMQ) only when the
        # device offers no MJPG. Captured through PyAV (libav v4l2 demuxer).
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
        if not self._can_read_frame():
            self.release()
            raise RuntimeError(f"[V4L2Camera] Camera {self._cam_topic} failed to initialize or read frames.")
        else:
            logger_mp.info(str(self))

    def __str__(self):
        return (
            f"[V4L2Camera: {self._cam_topic}] initialized with "
            f"{self._img_shape[0]}x{self._img_shape[1]} @ {self._fps} FPS.\n"
            f"ZMQ: {'enabled, zmq port=' + str(self._zmq_port) if self._enable_zmq else 'disabled'}; "
            f"WebRTC: {'enabled, webrtc port=' + str(self._webrtc_port) if self._enable_webrtc else 'disabled'}"
        )
        
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
            if self._enable_webrtc:
                self._webrtc_buffer.write(_turbojpeg.decode(jpeg_bytes))
        else:
            # YUYV source: decode to BGR (libswscale); re-encode for ZMQ.
            frames = packet.decode()
            if not frames:
                return
            bgr_numpy = frames[0].to_ndarray(format="bgr24")
            if self._enable_webrtc:
                self._webrtc_buffer.write(bgr_numpy)
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
        logger_mp.info(f"[V4L2Camera] Released {self._cam_topic}")

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
        sysfs_path = f"/sys/class/video4linux/{os.path.basename(video_path)}/device"
        return os.path.realpath(sysfs_path)

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

        d = physical_path
        for _ in range(10):
            if not d or d == "/" or not os.path.isdir(d):
                break
            vid = _read(os.path.join(d, "idVendor"))
            pid = _read(os.path.join(d, "idProduct"))
            if vid and pid:
                return {
                    "serial_number": _read(os.path.join(d, "serial")),
                    "bcd_device": _read(os.path.join(d, "bcdDevice")),
                    "vid_pid": f"{vid}:{pid}",
                    "name": _read(os.path.join(d, "product")),
                    "manufacturer": _read(os.path.join(d, "manufacturer")),
                }
            nxt = os.path.dirname(d)
            if nxt == d:
                break
            d = nxt
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
        base = "/sys/class/video4linux/"
        if not os.path.exists(base):
            return []
        out = []
        for name in sorted(os.listdir(base)):
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
        base = "/sys/class/video4linux/"
        if not os.path.exists(base):
            return []
        id_map = {r["video_path"]: r for r in cls._list_identifiers()}
        cards = []
        for name in sorted(os.listdir(base)):
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
    def from_config(cls, cam_topic, cam_cfg, base_kwargs):
        video_id = cam_cfg.get("video_id", "0")
        video_path = f"/dev/video{video_id}" if video_id else None
        physical_path = str(cam_cfg.get("physical_path")) if cam_cfg.get("physical_path") else None
        serial_number = str(cam_cfg.get("serial_number")) if cam_cfg.get("serial_number") else None
        _bcd = cam_cfg.get("bcd_device")
        bcd_device = f"{_bcd:04d}" if type(_bcd) is int else (str(_bcd) if _bcd else None)
        vid_pid = str(cam_cfg.get("vid_pid")) if cam_cfg.get("vid_pid") else None

        # Self-contained resolution: V4L2 harvests its OWN sysfs identifiers (no
        # CameraFinder). A camera libuvc cannot open is still bindable by
        # physical_path / serial_number / bcd_device / vid_pid.
        ids = cls._list_identifiers()

        if physical_path is not None:
            vpath = cls._resolve_vpath(ids, "physical_path", physical_path)
            if vpath is None:
                logger_mp.error(f"[Image Server] Cannot find V4L2Camera for {cam_topic} with physical path {physical_path}")
            else:
                return cls(cam_topic, vpath, **base_kwargs)

        # once you specify either `physical_path` or `serial_number`, the system will no longer fall back to searching by `video_id`.
        # ——— even if no camera matches the given path/serial.
        if serial_number is not None:
            vpath = cls._resolve_vpath(ids, "serial_number", serial_number)
            if vpath is None:
                logger_mp.error(f"[Image Server] Cannot find V4L2Camera for {cam_topic} with serial number {serial_number}")
                return None
            return cls(cam_topic, vpath, **base_kwargs)

        if bcd_device is not None:
            vpath = cls._resolve_vpath(ids, "bcd_device", bcd_device)
            if vpath is None:
                logger_mp.error(f"[Image Server] Cannot find V4L2Camera for {cam_topic} with bcd_device {bcd_device}")
                return None
            return cls(cam_topic, vpath, **base_kwargs)

        if vid_pid is not None:
            vpath = cls._resolve_vpath(ids, "vid_pid", vid_pid)
            if vpath is None:
                logger_mp.error(f"[Image Server] Cannot find V4L2Camera for {cam_topic} with vid_pid {vid_pid}")
                return None
            return cls(cam_topic, vpath, **base_kwargs)

        if not any(r["video_path"] == video_path for r in ids):
            logger_mp.error(f"[Image Server] Cannot find V4L2Camera for {cam_topic} with video_id {video_id}")
            return None
        return cls(cam_topic, video_path, **base_kwargs)

class GStreamerCamera(BaseCamera):
    def __init__(self, cam_topic, gst_pipeline, img_shape, fps,
                 enable_zmq=True, zmq_port=55555, enable_webrtc=False, webrtc_port=60001, webrtc_codec=None):
        super().__init__(cam_topic, img_shape, fps, enable_zmq, zmq_port, enable_webrtc, webrtc_port, webrtc_codec)
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
        return (
            f"[GStreamerCamera: {self._cam_topic}] initialized with "
            f"{self._img_shape[0]}x{self._img_shape[1]} @ {self._fps} FPS.\n"
            f"ZMQ: {'enabled, zmq port=' + str(self._zmq_port) if self._enable_zmq else 'disabled'}; "
            f"WebRTC: {'enabled, webrtc port=' + str(self._webrtc_port) if self._enable_webrtc else 'disabled'}"
        )

    @staticmethod
    def get_gstreamer_module():
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            if not Gst.is_initialized():
                Gst.init(None)
            return Gst
        except Exception as e:
            raise RuntimeError(
                "PyGObject/GStreamer not installed. Install with:\n"
                "    sudo apt install python3-gi python3-gst-1.0 "
                "gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad"
            ) from e

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
        if self._enable_webrtc:
            self._webrtc_buffer.write(_turbojpeg.decode(jpeg_bytes))

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
        logger_mp.info(f"[GStreamerCamera] Released {self._cam_topic}")

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
            mdev = re.match(r"device\.path\s*=\s*(/dev/video\d+)", s)
            mlaunch = re.search(r"gst-launch-1\.0\s+(.+)", s)
            if mname:
                name = mname.group(1).strip()
            elif mdev:
                device = mdev.group(1)
            elif mlaunch:
                launch = mlaunch.group(1).strip()
                launch = re.sub(r"\s*!\s*\.\.\.\s*$", "", launch).rstrip("! ").strip()
                if device and "device=" not in launch:
                    launch = re.sub(r"^v4l2src\b", f"v4l2src device={device}", launch)
                cards.append({
                    "type": "gstreamer",
                    "name": name,
                    "video_path": device,
                    "gst_pipeline": f"{launch} ! image/jpeg ! appsink name=sink",
                })
                name, device = None, None
        return cards

    @classmethod
    def from_config(cls, cam_topic, cam_cfg, base_kwargs):
        gst_pipeline = cam_cfg.get("gst_pipeline")
        if not gst_pipeline:
            logger_mp.error(f"[Image Server] type 'gstreamer' for {cam_topic} requires a 'gst_pipeline' (must contain appsink).")
            return None
        return cls(cam_topic, gst_pipeline, **base_kwargs)

class IsaacSimCamera(BaseCamera):
    def __init__(self, cam_topic, img_shape, fps,
                 enable_zmq=True, zmq_port=55555, enable_webrtc=False, webrtc_port=60001, webrtc_codec=None,
                 image_source="head", binocular=False):
        """
        IsaacSim camera that reads from shared memory.

        Args:
            cam_topic: camera topic name
            img_shape: image shape [height, width]
            fps: frames per second
            enable_zmq: enable ZMQ publishing
            zmq_port: ZMQ port
            enable_webrtc: enable WebRTC publishing
            webrtc_port: WebRTC port
            webrtc_codec: WebRTC codec preference
            image_source: which image to read from shared memory ("head", "left", "right")
            binocular: if True and image_source=="head", concatenate left+right for binocular vision
        """
        super().__init__(cam_topic, img_shape, fps, enable_zmq, zmq_port, enable_webrtc, webrtc_port, webrtc_codec)
        from tools.shared_memory_utils import MultiImageReader # https://github.com/unitreerobotics/unitree_sim_isaaclab/tree/main/tools
        self.multi_image_reader = MultiImageReader()
        self._image_source = image_source  # "head", "left", or "right"
        self._binocular = binocular
        # For IsaacSim cameras, set ready immediately since the camera object is initialized
        # and will wait for shared memory data in _update_frame
        self._ready.set()
        logger_mp.info(str(self))

    def __str__(self):
        mode = "binocular" if self._binocular else "monocular"
        return (
            f"[IsaacSimCamera: {self._cam_topic}] initialized with "
            f"{self._img_shape[0]}x{self._img_shape[1]} @ {self._fps} FPS, source='{self._image_source}', mode='{mode}'.\n"
            f"ZMQ: {'enabled, zmq port=' + str(self._zmq_port) if self._enable_zmq else 'disabled'}; "
            f"WebRTC: {'enabled, webrtc port=' + str(self._webrtc_port) if self._enable_webrtc else 'disabled'}"
        )

    def _update_frame(self):
        # Get the image data based on source and binocular settings
        frame_data = None
        if self._binocular:
            # For binocular cameras: concatenate left + right images
            left_img = self.multi_image_reader.read_single_image('left')
            right_img = self.multi_image_reader.read_single_image('right')
            logger_mp.debug(f"[IsaacSimCamera] {self._cam_topic} - left: {left_img is not None}, right: {right_img is not None}")

            if left_img is not None and right_img is not None:
                frame_data = np.hstack([left_img, right_img])
                logger_mp.debug(f"[IsaacSimCamera] {self._cam_topic} - concatenated binocular frame: {frame_data.shape}")
        else:
            # For monocular cameras: use the specified source directly
            frame_data = self.multi_image_reader.read_single_image(self._image_source)
            if frame_data is None:
                logger_mp.debug(f"[IsaacSimCamera] {self._cam_topic} - no data for source '{self._image_source}'")

        # Publish the frame data only if we have valid data
        if frame_data is not None:
            # For ZMQ: encode to JPEG bytes
            if self._enable_zmq:
                self._zmq_buffer.write(_turbojpeg.encode(frame_data))

            # For WebRTC: use BGR frames directly
            if self._enable_webrtc:
                self._webrtc_buffer.write(frame_data)
            else:
                logger_mp.warning(f"[IsaacSimCamera] Failed to encode to WebRTC for {self._cam_topic}")
            if not self._ready.is_set():
                self._ready.set()
        else:
            logger_mp.debug(f"[IsaacSimCamera] No data available for {self._cam_topic}, frame_data is None")
        # If no data is available, just return silently and wait for next frame

    def release(self):
        if hasattr(self, 'multi_image_reader') and self.multi_image_reader is not None:
            self.multi_image_reader.close()
        self.multi_image_reader = None
        logger_mp.info(f"[IsaacSimCamera] Released {self._cam_topic}")

    @classmethod
    def scan(cls):
        """IsaacSim cameras are virtual (shared-memory / topic-convention based);
        there is no physical device to discover."""
        return []

    @classmethod
    def from_config(cls, cam_topic, cam_cfg, base_kwargs):
        # Check if binocular mode is enabled
        binocular = cam_cfg.get("binocular", False)

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

        return cls(cam_topic, image_source=image_source, binocular=binocular, **base_kwargs)
# ========================================================
# image server
# ========================================================
class ImageServer:
    def __init__(self, cam_config, enable_realsense=False, enable_isaacsim=False):
        _apply_webrtc_config(cam_config)
        self._cam_config = cam_config
        self._enable_realsense = enable_realsense
        self._enable_isaacsim = enable_isaacsim
        self._stop_event = threading.Event()
        self._cameras: dict[str, BaseCamera] = {}

        if not self._enable_isaacsim:
            reload_uvcvideo_module()

        self._responser = ZMQ_Responser(self._cam_config)
        self._zmq_publisher_manager = ZMQ_PublisherManager.get_instance()
        self._webrtc_publisher_manager = WebRTC_PublisherManager.get_instance()
        self._publisher_threads = []  # keep references for graceful join

        try:
            # Load cameras from self.cam_config
            for cam_topic, cam_cfg in self._cam_config.items():
                if not cam_cfg.get("enable_zmq", False) and not cam_cfg.get("enable_webrtc", False):
                    continue

                enable_zmq = cam_cfg.get("enable_zmq", False)
                zmq_port = cam_cfg.get("zmq_port", None)
                enable_webrtc = cam_cfg.get("enable_webrtc", False)
                webrtc_port = cam_cfg.get("webrtc_port", None)
                webrtc_codec = cam_cfg.get("webrtc_codec", None)
                cam_type = cam_cfg.get("type", "uvc").lower()
                if self._enable_isaacsim and cam_type!="isaacsim":
                    cam_type = "isaacsim"
                img_shape = cam_cfg.get("image_shape", None)
                fps = cam_cfg.get("fps", 30)
                base_kwargs = dict(
                    img_shape=img_shape, fps=fps,
                    enable_zmq=enable_zmq, zmq_port=zmq_port,
                    enable_webrtc=enable_webrtc, webrtc_port=webrtc_port, webrtc_codec=webrtc_codec,
                )

                if cam_type == "v4l2":
                    self._cameras[cam_topic] = V4L2Camera.from_config(cam_topic, cam_cfg, base_kwargs)
                elif cam_type == "realsense":
                    self._cameras[cam_topic] = RealSenseCamera.from_config(cam_topic, cam_cfg, base_kwargs, self._enable_realsense)
                elif cam_type == "uvc":
                    self._cameras[cam_topic] = UVCCamera.from_config(cam_topic, cam_cfg, base_kwargs)
                elif cam_type == "gstreamer":
                    self._cameras[cam_topic] = GStreamerCamera.from_config(cam_topic, cam_cfg, base_kwargs)
                elif cam_type == "isaacsim":
                    self._cameras[cam_topic] = IsaacSimCamera.from_config(cam_topic, cam_cfg, base_kwargs)
                else:
                    logger_mp.error(f"[Image Server] Unknown camera type {cam_type} for {cam_topic}, skipping...")
                    continue
        except Exception as e:
            logger_mp.error(f"[Image Server] Initialization failed: {e}")
            self._clean_up()
            raise

        logger_mp.info("[Image Server] Image server has started, waiting for client connections...")

    def _update_frames(self, cam_topic: str, camera: BaseCamera):
        try:
            interval = 1.0 / camera.get_fps()
            next_frame_time = time.monotonic()
            while not self._stop_event.is_set():
                try:
                    camera._update_frame()
                except Exception as e:
                    logger_mp.error(f"[Image Server] Error updating frame for {cam_topic} camera")
                    self._stop_event.set()
                    break
                next_frame_time += interval
                sleep_time = next_frame_time - time.monotonic()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    next_frame_time = time.monotonic()
        except Exception as e:
            logger_mp.error(f"[Image Server] Failed to update frames for {cam_topic} camera: {e}")
            self._stop_event.set()

    def _zmq_pub(self, cam_topic: str, camera: BaseCamera):
        try:
            interval = 1.0 / camera.get_fps()
            next_frame_time = time.monotonic()

            while not self._stop_event.is_set():
                jpeg_bytes = camera.get_jpeg_bytes()
                if jpeg_bytes is not None:
                    self._zmq_publisher_manager.publish(jpeg_bytes, camera.get_zmq_port())
                else:
                    logger_mp.warning(f"[Image Server] {cam_topic} returned no frame.")
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
            self._stop_event.set()
    
    def _webrtc_pub(self, cam_topic: str, camera: BaseCamera):
        try:
            interval = 1.0 / camera.get_fps()
            webrtc_codec = camera.get_webrtc_codec()
            next_frame_time = time.monotonic()
            while not self._stop_event.is_set():
                bgr_frame = camera.get_bgr_frame()

                if bgr_frame is not None:
                    self._webrtc_publisher_manager.publish(bgr_frame, camera.get_webrtc_port(), codec_pref=webrtc_codec)
                else:
                    logger_mp.info(f"[Image Server] {cam_topic} returned no frame.")
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
            self._stop_event.set()

    def _clean_up(self):
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
            logger_mp.info(f"[Image Server] {camera_topic} is ready.")
        
        for camera_topic, camera in self._cameras.items():
            if camera.enable_webrtc():
                t = threading.Thread(target=self._webrtc_pub, args=(camera_topic, camera), daemon=True)
                t.start()
                self._publisher_threads.append(t)

            if camera.enable_zmq():
                t = threading.Thread(target=self._zmq_pub, args=(camera_topic, camera), daemon=True)
                t.start()
                self._publisher_threads.append(t)

    def wait(self):
        self._stop_event.wait()
        self._clean_up()

    def stop(self):
        self._stop_event.set()

# ========================================================
# utility functions
# ========================================================
def signal_handler(server, signum, frame):
    logger_mp.info(f"[Image Server] Received signal {signum}, initiating graceful shutdown...")
    server.stop()

def set_performance_mode(cores=[0, 1, 2]):
    import psutil
    try:
        p = psutil.Process(os.getpid())
        
        # Set CPU affinity for the process and all its threads
        p.cpu_affinity(cores)
        logger_mp.info(f"[Performance] CPU Affinity locked to: {cores}")

    except psutil.AccessDenied:
        logger_mp.warning("[Performance] Access Denied: Run as sudo for full optimization")
    except Exception as e:
        logger_mp.error(f"[Performance] Error: {e}")

def run_isaacsim_server():
    # Load config file, start image server
    try:
        with open(CONFIG_PATH, "r") as f:
            cam_config = yaml.safe_load(f)
    except Exception as e:
        logger_mp.error(f"Failed to load configuration file at {CONFIG_PATH}: {e}")
        exit(1)
    # start image server
    server = ImageServer(cam_config, enable_realsense=False, enable_isaacsim=True)
    server.start()
    return server

def main():
    logger_mp.info(
        "\n====================== Image Server Startup Guide ======================\n"
        "Please first read this repo's README.md to learn how to configure and use the teleimager.\n"
        "To discover connected cameras, run the following command:\n"
        "\n"
        "    teleimager-server --cf\n"
        "\n"
        "The '--cf' flag means 'camera find'.\n"
        "This will list all detected cameras and their details (video paths, serial numbers and physical path etc.).\n"
        "Use that information to fill in your 'cam_config_server.yaml' file.\n"
        "Once configured, you can start the image server with:\n"
        "\n"
        "    teleimager-server\n"
        "\n"
        "Note:\n"
        " - If you have RealSense cameras, add the '--rs' flag to enable RealSense support.\n"
        " - Make sure you have proper permissions to access the camera devices (e.g., run with sudo or set udev rules).\n"
        "=========================================================================="
    )

    # command line args
    parser = argparse.ArgumentParser()
    parser.add_argument('--cf', action='store_true', help='Camera-finder mode: scan and print all connected cameras, then exit.')
    parser.add_argument('--uvc', action='store_true', help='In --cf, include the UVC (libuvc -> pyuvc) backend in the scan.')
    parser.add_argument('--v4l2', action='store_true', help='In --cf, include the V4L2 (PyAV) backend in the scan.')
    parser.add_argument('--gst', action='store_true', help='In --cf, include the GStreamer backend in the scan.')
    parser.add_argument('--rs', action='store_true', help='In --cf, include the RealSense (pyrealsense2) backend in the scan; also enables RealSense at server runtime.')
    parser.add_argument('--isaacsim', action='store_true', help='Run the server in IsaacSim mode (frames from shared memory instead of physical cameras).')
    parser.add_argument('--no-affinity', action='store_false', dest='affinity', help='Do not pin the process to specific CPU cores.')
    args = parser.parse_args()

    if args.affinity:
        set_performance_mode(cores=[0, 1, 2])

    # if enable camera finder mode, just print cameras info and exit
    if args.cf:
        logger_mp.info("args:%s", args)
        CameraFinder(enable_uvc=args.uvc,
                     enable_v4l2=args.v4l2,
                     enable_gstreamer=args.gst,
                     enable_realsense=args.rs)
        exit(0)

    # Load config file, start image server
    try:
        with open(CONFIG_PATH, "r") as f:
            cam_config = yaml.safe_load(f)
    except Exception as e:
        logger_mp.error(f"Failed to load configuration file at {CONFIG_PATH}: {e}")
        exit(1)

    # start image server
    server = ImageServer(cam_config, enable_realsense=args.rs)
    server.start()

    # graceful shutdown handling
    signal.signal(signal.SIGINT, functools.partial(signal_handler, server))
    signal.signal(signal.SIGTERM, functools.partial(signal_handler, server))

    logger_mp.info("[Image Server] Running... Press Ctrl+C to exit.")
    server.wait()

    # usbhub plugout may cause block process exit, no better solution for now
    time.sleep(0.5)
    os.killpg(os.getpgrp(), 9)

if __name__ == "__main__":
    main()
