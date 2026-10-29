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

"""Serve browser previews and publish BGR frames through aiohttp and aiortc."""

from importlib import resources
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import asyncio
import fractions
import json
import os
import queue
import re
import ssl
import threading
import time

from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.codecs import h264
from aiortc.contrib.media import MediaRelay
from aiortc.rtcrtpsender import RTCRtpSender
import av
import logging_mp
import numpy as np

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "teleimager"
CERT_PEM_PATH = CONFIG_DIR / "cert.pem"
KEY_PEM_PATH = CONFIG_DIR / "key.pem"


_GOP_LENGTH = 60


def _apply_webrtc_config(server_config):
    """Apply process-wide codec bitrate limits and the software H264 encoder.

    Installs the libx264 encoding method on aiortc's H264Encoder and updates
    module-level bitrate and GOP settings used by publisher threads.
    """
    global _GOP_LENGTH
    h264.H264Encoder._encode_frame = jetson_software_encode_frame
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


def jetson_software_encode_frame(self, frame: av.VideoFrame, force_keyframe: bool):
    """Encode a video frame with libx264 and yield H264 bitstream units.

    Used as aiortc's H264Encoder method, with ultrafast, zerolatency, and one
    encoder thread at a fixed 30 FPS encoder time base. Recreates the codec on
    size changes and forces keyframes according to the configured GOP length.
    """
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


UNITREE_LOGO_SVG = resources.files("teleimager").joinpath("unitree_logo.svg").read_text(encoding="utf-8")


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


class BGRArrayVideoStreamTrack(MediaStreamTrack):
    """Expose queued BGR images as video frames with a 90 kHz RTP time base."""

    kind = "video"

    def __init__(self):
        """Initialize a single-frame asyncio queue and timestamp state."""
        super().__init__()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._start_time = None
        self._pts = 0

    async def recv(self) -> av.VideoFrame:
        """Wait asynchronously for the next queued video frame."""
        frame = await self._queue.get()
        return frame

    def push_frame(self, bgr_numpy: np.ndarray, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Convert a BGR array and schedule it on the destination event loop.

        Assigns timestamps from elapsed wall time using a 90 kHz clock. Queue
        replacement runs on the event loop and drops an older queued frame.
        """
        if bgr_numpy is None:
            return

        try:
            video_frame = av.VideoFrame.from_ndarray(bgr_numpy, format="bgr24")

            if self._start_time is None:
                self._start_time = time.time()
                self._pts = 0
            else:
                self._pts = int((time.time() - self._start_time) * 90000)

            video_frame.pts = self._pts
            video_frame.time_base = fractions.Fraction(1, 90000)

        except Exception as e:
            logger_mp.debug(f"[Teleimager] Conversion failed: {e}")
            return

        # Mutate the asyncio queue only on its owning event loop.
        target_loop = loop or asyncio.get_event_loop()
        if target_loop.is_closed():
            return

        def _put():
            try:
                if self._queue.full():
                    self._queue.get_nowait()
                self._queue.put_nowait(video_frame)
            except Exception:
                pass

        target_loop.call_soon_threadsafe(_put)


class WebRTC_PublisherThread(threading.Thread):
    """Run an HTTPS preview and WebRTC peers on a dedicated thread and event loop.

    Threads share input arrays without process serialization. A MediaRelay
    shares the source track among peers; each peer negotiates its own codec.
    """

    def __init__(self, port: int, host: str = "0.0.0.0", codec_pref: str = None):
        """Prepare HTTPS routes, peer state, and a single-frame input queue."""
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

        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/client.js", self._javascript)
        self._app.router.add_post("/offer", self._offer)

        self._app.router.add_options("/", self._options)
        self._app.router.add_options("/client.js", self._options)
        self._app.router.add_options("/offer", self._options)

    async def _index(self, request: web.Request) -> web.Response:
        """Return the browser preview page."""
        return web.Response(content_type="text/html", text=INDEX_HTML)

    async def _javascript(self, request: web.Request) -> web.Response:
        """Return the browser negotiation and codec fallback script."""
        return web.Response(content_type="application/javascript", text=CLIENT_JS)

    async def _options(self, request):
        """Return CORS headers for a browser preflight request."""
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )

    def _error_response(self, status: int, message: str) -> web.Response:
        """Return a JSON error with the signaling endpoint's CORS headers."""
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
        """Negotiate an SDP offer and attach a relayed source track to the new peer."""
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

        # Relay source frames to peers; this does not share their encoders.
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
        """Remove a peer from the active set and close its connection."""
        self._pcs.discard(pc)
        try:
            await pc.close()
        except: pass

    def wait_for_start(self, timeout=1.0):
        """Wait for the HTTPS listener and return whether startup completed."""
        return self._start_event.wait(timeout=timeout)

    def run(self):
        """Own the asyncio loop, start HTTPS, forward frames, and clean up on exit."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _main():
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()

            # Bind asyncio track state to the publisher's event loop.
            self._bgr_track = BGRArrayVideoStreamTrack()
            self._relay = MediaRelay()

            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(CERT_PEM_PATH, KEY_PEM_PATH)
            site = web.TCPSite(self._runner, self._host, self._port, ssl_context=ssl_context)
            await site.start()
            self._start_event.set()

            while not self._stop_event.is_set():
                try:
                    if not self._frame_queue.empty():
                        frame = self._frame_queue.get_nowait()
                        self._bgr_track.push_frame(frame, loop=self._loop)

                    # Yield so signaling and WebRTC packet tasks can progress.
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
        """Close active peers and release the aiohttp runner on the worker loop."""
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
        """Queue a BGR array for the worker, replacing an older queued frame if full."""
        if not self._frame_queue.full():
            self._frame_queue.put(data)
        else:
            try:
                self._frame_queue.get_nowait()
                self._frame_queue.put(data)
            except: pass

    def stop(self):
        """Request shutdown and wait briefly for the worker thread."""
        self._stop_event.set()
        self.join(timeout=1.0)


class WebRTC_PublisherManager:
    """Reuse one WebRTC publisher worker per local (host, port) endpoint."""

    _instance: Optional["WebRTC_PublisherManager"] = None
    _publisher_threads: Dict[Tuple[str, int], WebRTC_PublisherThread] = {}
    _lock = threading.Lock()
    _running = True

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "WebRTC_PublisherManager":
        """Return or create the process-wide WebRTC publisher manager under its lock."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _create_publisher(self, port: int, host: str, codec_pref: str):
        """Start an endpoint worker and log if HTTPS readiness times out."""
        t = WebRTC_PublisherThread(port, host, codec_pref)
        t.start()
        if not t.wait_for_start(timeout=10.0):
             logger_mp.error(f"[Teleimager] WebRTC publisher on {host}:{port} failed to start.")
        return t

    def _get_publisher(self, port, host, codec_pref):
        """Return or create an endpoint worker while holding the manager lock."""
        key = (host, port)
        with self._lock:
            if key not in self._publisher_threads:
                self._publisher_threads[key] = self._create_publisher(port, host, codec_pref)
            return self._publisher_threads[key]

    def publish(self, data: Any, port: int, host: str = "0.0.0.0", codec_pref: str = None) -> None:
        """Queue BGR data, logging failures and ignoring calls after close."""
        if not self._running: return
        try:
            pub = self._get_publisher(port, host, codec_pref)
            pub.send(data)
        except Exception as e:
            logger_mp.error(f"[Teleimager] Unexpected error in publish: {e}")
            pass

    def close(self) -> None:
        """Mark the manager closed and stop and remove all WebRTC workers."""
        self._running = False
        with self._lock:
            for key, pub in list(self._publisher_threads.items()):
                try:
                    pub.stop()
                except Exception: pass
            self._publisher_threads.clear()
