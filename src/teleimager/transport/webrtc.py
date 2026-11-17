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

"""Serve browser previews and publish video frames through aiohttp and aiortc."""

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

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "teleimager"
CERT_PEM_PATH = CONFIG_DIR / "cert.pem"
KEY_PEM_PATH = CONFIG_DIR / "key.pem"


_GOP_LENGTH = 60  # Frames between keyframes; set to 2 s at the configured fps.
_WEBRTC_FPS = 30  # Encoder frame rate; set from the WebRTC cameras' fps.

# H.264 encoders as (FFmpeg name, options). A hardware encoder is used when
# this FFmpeg build has it and it opens at startup; otherwise libx264 (CPU).
_SW_H264 = ("libx264", {"preset": "ultrafast", "tune": "zerolatency", "threads": "1"})
_HW_H264 = [("h264_nvenc", {"preset": "p1", "tune": "ull", "zerolatency": "1", "bf": "0", "delay": "0"}),
            ("h264_nvmpi", {}),
            ("h264_nvv4l2enc", {})]
_H264_ENCODER = _SW_H264


def _open_h264(name, options, width=640, height=480, bitrate=1_000_000):
    """Open an H.264 encoder; the default size and bitrate form the startup probe frame."""
    codec = av.CodecContext.create(name, "w")
    codec.width = width
    codec.height = height
    codec.bit_rate = bitrate
    codec.pix_fmt = "yuv420p"
    codec.framerate = fractions.Fraction(_WEBRTC_FPS, 1)
    codec.time_base = fractions.Fraction(1, _WEBRTC_FPS)
    codec.options = {**options, "g": str(_GOP_LENGTH)}
    codec.open()
    return codec


def _apply_webrtc_config(server_config):
    """Apply process-wide codec limits, frame rate, GOP and the H.264 encoder.

    Installs h264_encode_frame on aiortc's H264Encoder and updates module-level
    bitrate settings. With WebRTC cameras, the encoder frame rate follows their
    fps (30 when they differ), the GOP defaults to 2 s, and an H.264 hardware
    encoder that opens on a probe frame replaces libx264.
    """
    global _GOP_LENGTH, _WEBRTC_FPS, _H264_ENCODER
    h264.H264Encoder._encode_frame = h264_encode_frame
    cfg = (server_config or {}).get("webrtc", {})
    bitrate = cfg.get("bitrate", {})
    from aiortc.codecs import vpx
    for key, attr in (("min", "MIN_BITRATE"), ("default", "DEFAULT_BITRATE"), ("max", "MAX_BITRATE")):
        if key in bitrate and int(bitrate[key]) >= 100_000:
            setattr(h264, attr, int(bitrate[key]))
            setattr(vpx, attr, int(bitrate[key]))
    cameras = [c for c in (server_config or {}).get("camera", {}).values() if c.get("enable_webrtc")]
    if not cameras:
        return

    fps = {int(c.get("fps", 30)) for c in cameras}
    _WEBRTC_FPS = fps.pop() if len(fps) == 1 else 30
    gop = cfg.get("gop_length")
    _GOP_LENGTH = int(gop) if gop and int(gop) > 0 else _WEBRTC_FPS * 2

    codecs = {(c.get("webrtc_codec") or "h264").lower() for c in cameras}
    if codecs - {"vp8"}:
        for name, options in _HW_H264:
            try:
                list(_open_h264(name, options).encode(av.VideoFrame(640, 480, format="yuv420p")))
                _H264_ENCODER = (name, options)
                break
            except Exception:
                logger_mp.debug(f"[Teleimager] H.264 hardware encoder {name} not available")
    encoders = ", ".join("vp8 (libvpx)" if c == "vp8" else f"h264 ({_H264_ENCODER[0]})" for c in sorted(codecs))
    logger_mp.info(f"[Teleimager] WebRTC encoder: {encoders}, bitrate min/default/max="
                   f"{h264.MIN_BITRATE}/{h264.DEFAULT_BITRATE}/{h264.MAX_BITRATE}, gop={_GOP_LENGTH}, fps={_WEBRTC_FPS}")


def h264_encode_frame(self, frame: av.VideoFrame, force_keyframe: bool):
    """Encode a video frame with the selected H.264 encoder and yield bitstream units.

    Used as aiortc's H264Encoder method. Recreates the codec on size changes,
    falls back to libx264 when the hardware encoder cannot open (for example
    at its session limit), and forces keyframes every GOP length.
    """
    if self.codec and (frame.width != self.codec.width or frame.height != self.codec.height):
        self.codec = None

    if self.codec is None:
        for name, options in (_H264_ENCODER, _SW_H264):
            try:
                self.codec = _open_h264(name, options, frame.width, frame.height, self.target_bitrate)
                if name != _H264_ENCODER[0]:
                    logger_mp.info(f"[Teleimager] H.264 encoder: {name} ({frame.width}x{frame.height}@{_WEBRTC_FPS})")
                break
            except Exception as e:
                logger_mp.warning(f"[Teleimager] {name} failed to open: {e}")
        if self.codec is None:
            return
        self.frame_count = 0
        force_keyframe = True

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
var FALLBACK_MS = 20000;

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
            // H.264 -> VP8 fallback: if no frames in FALLBACK_MS, reconnect with VP8
            if (!_fallback) {
                var t0 = v.currentTime;
                setTimeout(function() {
                    if (v.currentTime === t0 && !_fallback) {
                        stop();
                        _fallback = true;
                        start();
                    }
                }, FALLBACK_MS);
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


class VideoFrameStreamTrack(MediaStreamTrack):
    """Expose queued video frames, latest first, with a 90 kHz RTP time base."""

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

    def push_frame(self, frame: av.VideoFrame, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Timestamp a video frame and schedule it on the destination event loop.

        Assigns timestamps from elapsed monotonic time using a 90 kHz clock, so
        MediaRelay sees consistent PTS. Queue replacement runs on the event
        loop and drops an older queued frame.
        """
        if frame is None:
            return

        try:
            if self._start_time is None:
                self._start_time = time.monotonic()
                self._pts = 0
            else:
                self._pts = int((time.monotonic() - self._start_time) * 90000)

            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, 90000)

        except Exception as e:
            logger_mp.debug(f"[Teleimager] Timestamping failed: {e}")
            return

        # Mutate the asyncio queue only on its owning event loop.
        target_loop = loop or asyncio.get_event_loop()
        if target_loop.is_closed():
            return

        def _put():
            try:
                if self._queue.full():
                    self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except Exception:
                pass

        target_loop.call_soon_threadsafe(_put)


class WebRTC_PublisherThread(threading.Thread):
    """Run an HTTPS preview and WebRTC peers on a dedicated thread and event loop.

    Threads share input frames without process serialization. A MediaRelay
    shares the source track among peers; each peer negotiates its own codec.
    The connected event is set while at least one peer is open.
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
        self._connected = threading.Event()  # Set while at least one peer is open.
        self._start_event = threading.Event()
        self._stop_event = threading.Event()
        self._frame_queue = queue.Queue(maxsize=1)

        self._video_track: Optional[VideoFrameStreamTrack] = None
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
        self._connected.set()

        # Relay source frames to peers; this does not share their encoders.
        if self._video_track and self._relay:
            try:
                relayed_track = self._relay.subscribe(self._video_track)
                transceiver = pc.addTransceiver(relayed_track, direction="sendonly")
                capabilities = RTCRtpSender.getCapabilities("video")
                client_codec = params.get("codec")
                pref = (client_codec or self._codec_pref or "h264").lower()

                if pref == "h264":
                    h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
                    if h264_codecs:
                        transceiver.setCodecPreferences(h264_codecs)
                        logger_mp.info(f"[Teleimager] Preferred H264 for port:{self._port} (encoder: {_H264_ENCODER[0]})")
                    else:
                        logger_mp.warning(f"[Teleimager] H264 preferred but not found, using auto-negotiation for port:{self._port}")

                elif pref == "vp8":
                    vp8_codecs = [c for c in capabilities.codecs if c.mimeType == "video/VP8"]
                    if vp8_codecs:
                        transceiver.setCodecPreferences(vp8_codecs)
                        logger_mp.info(f"[Teleimager] Preferred VP8 for port:{self._port} (encoder: libvpx)")
                    else:
                        logger_mp.warning(f"[Teleimager] VP8 preferred but not found, using auto-negotiation for port:{self._port}")

                else:
                    h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
                    if h264_codecs:
                        transceiver.setCodecPreferences(h264_codecs)
                        logger_mp.info(f"[Teleimager] Preferred codec '{pref}' not found, falling back to H264 for port:{self._port} (encoder: {_H264_ENCODER[0]})")
                    else:
                        logger_mp.warning(f"[Teleimager] Preferred codec '{pref}' not found, using auto-negotiation for port:{self._port}")

            except Exception as e:
                logger_mp.error(f"[Teleimager] Relay subscription failed: {e}")
                await self._cleanup_pc(pc)
                return self._error_response(500, "Relay subscription failed")

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
        """Remove a peer from the active set, clear the connected event when none remain, and close it."""
        self._pcs.discard(pc)
        if not self._pcs:
            self._connected.clear()
        try:
            await pc.close()
        except: pass

    def wait_for_start(self, timeout=1.0):
        """Wait for the HTTPS listener and return whether startup completed."""
        return self._start_event.wait(timeout=timeout)

    def run(self):
        """Own the asyncio loop, start HTTPS, and clean up on exit.

        Frames reach the track through call_soon_threadsafe from send(), so
        the loop only polls for the stop request.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _main():
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()

            # Bind asyncio track state to the publisher's event loop.
            self._video_track = VideoFrameStreamTrack()
            self._relay = MediaRelay()

            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(CERT_PEM_PATH, KEY_PEM_PATH)
            site = web.TCPSite(self._runner, self._host, self._port, ssl_context=ssl_context)
            await site.start()
            self._start_event.set()

            # Forward a frame queued before the track existed.
            self._drain_frames()
            while not self._stop_event.is_set():
                await asyncio.sleep(0.1)

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
        """Close active peers, release the aiohttp runner, and cancel leftover tasks."""
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

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _drain_frames(self):
        """Move the queued frame, if any, onto the track; runs on the worker loop."""
        try:
            frame = self._frame_queue.get_nowait()
        except queue.Empty:
            return
        self._video_track.push_frame(frame, loop=self._loop)

    def send(self, data: Any):
        """Queue an av.VideoFrame and wake the worker loop, keeping only the latest frame."""
        if not self._frame_queue.full():
            self._frame_queue.put(data)
        else:
            try:
                self._frame_queue.get_nowait()
                self._frame_queue.put(data)
            except: pass

        loop = self._loop
        if loop is not None and self._video_track is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._drain_frames)
            except RuntimeError:
                pass

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

    def connected_event(self, port, host="0.0.0.0", codec_pref=None):
        """Return the endpoint's event that is set while a peer is connected.

        Creates and starts the endpoint worker on first use.
        """
        return self._get_publisher(port, host, codec_pref)._connected

    def publish(self, data: Any, port: int, host: str = "0.0.0.0", codec_pref: str = None) -> None:
        """Queue a video frame, logging failures and ignoring calls after close."""
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
