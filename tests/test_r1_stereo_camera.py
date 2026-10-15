from fractions import Fraction
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

import av
import cv2
import numpy as np
from aiortc.codecs.h264 import H264Encoder
from aiortc.rtp import RtpPacket

# Other TeleImager tests may already have initialized the process-wide logger.
with patch("logging_mp.basicConfig"):
    from teleimager.server import (
        BaseCamera, GStreamerCamera, IsaacSimCamera, RealSenseCamera,
        R1StereoCamera, TeleImageServer, UVCCamera, V4L2Camera,
    )


class R1StereoCameraTest(unittest.TestCase):
    def setUp(self):
        reload_driver = patch("teleimager.server.reload_uvcvideo_module")
        self.reload_driver = reload_driver.start()
        self.addCleanup(reload_driver.stop)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.sender.close)
        self.encoders = [H264Encoder(), H264Encoder()]
        self.sequences = [65530, 300]
        self.frame_numbers = [0, 0]

    def make_camera(self, **overrides):
        options = dict(
            cam_topic="head_camera", img_shape=[80, 240], fps=10,
            bind_host="127.0.0.1", source_host="127.0.0.1",
            left_port=0, right_port=0, receive_timeout=1.0, max_pair_delay=0.05,
        )
        options.update(overrides)
        options["image_shape"] = options.pop("img_shape")
        options["enable_zmq"] = True
        camera = R1StereoCamera(options.pop("cam_topic"), options)
        camera.enable_bgr_output()
        self.addCleanup(camera.release)
        return camera

    def send_eye(self, camera, eye, bgr, count=3, reorder=False, drop=False, ssrc=None):
        destination = camera._sockets[eye].getsockname()
        fragmented = False
        for _ in range(count):
            frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
            frame.pts = self.frame_numbers[eye]
            frame.time_base = Fraction(1, 10)
            self.frame_numbers[eye] += 1
            payloads, timestamp = self.encoders[eye].encode(frame, force_keyframe=True)
            datagrams = []
            for i, payload in enumerate(payloads):
                fragmented |= payload[0] & 31 == 28
                packet = RtpPacket(
                    payload_type=96,
                    sequence_number=self.sequences[eye] & 65535,
                    timestamp=(timestamp + (100_000 if eye == 0 else 3_000_000_000)) & 0xFFFFFFFF,
                )
                self.sequences[eye] += 1
                packet.ssrc = ssrc if ssrc is not None else eye + 1
                packet.marker = int(i == len(payloads) - 1)
                packet.payload = payload
                datagrams.append(packet.serialize())
            if reorder and len(datagrams) > 2:
                datagrams[1], datagrams[2] = datagrams[2], datagrams[1]
            if drop and len(datagrams) > 2:
                del datagrams[1]
            for data in datagrams:
                self.sender.sendto(data, destination)
        return fragmented

    def test_decodes_reordered_rtp_and_pads_each_eye(self):
        camera = self.make_camera()
        rng = np.random.RandomState(42)
        left = rng.randint(0, 256, (64, 80, 3), dtype=np.uint8)
        right = left.copy()
        left[16:48, 20:60] = (220, 30, 20)
        right[16:48, 20:60] = (20, 40, 220)
        self.sender.sendto(b"not RTP", camera._sockets[0].getsockname())
        self.assertTrue(self.send_eye(camera, 0, left, reorder=True))
        self.assertTrue(self.send_eye(camera, 1, right, reorder=True))
        camera._update_frame()

        self.assertTrue(camera.wait_until_ready(0))
        output = camera.get_bgr_frame()
        self.assertEqual(output.shape, (80, 240, 3))
        np.testing.assert_allclose(output[40, 60], (220, 30, 20), atol=12)
        np.testing.assert_allclose(output[40, 180], (20, 40, 220), atol=12)
        self.assertFalse(output[:, :10].any())
        self.assertFalse(output[:, 110:130].any())
        self.assertFalse(output[:, 230:].any())
        jpeg = cv2.imdecode(np.frombuffer(camera.get_jpeg_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(jpeg.shape, output.shape)

    def test_stale_eye_is_not_paired_with_a_new_eye(self):
        camera = self.make_camera()
        color = np.full((64, 80, 3), 100, dtype=np.uint8)
        errors = []

        def receive():
            try:
                camera._update_frame()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=receive)
        thread.start()
        self.addCleanup(thread.join, 1.5)
        self.send_eye(camera, 0, color)
        deadline = time.monotonic() + 0.5
        while camera._receivers[0]["pending"] is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(camera._receivers[0]["pending"])
        time.sleep(0.08)
        self.send_eye(camera, 1, color)
        time.sleep(0.02)
        self.assertFalse(camera.wait_until_ready(0))
        self.send_eye(camera, 0, color)
        thread.join(1.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(camera.wait_until_ready(0))

    def test_packet_loss_recovers_on_later_keyframes(self):
        camera = self.make_camera(receive_timeout=2.0)
        noise = np.random.RandomState(12).randint(0, 256, (64, 80, 3), dtype=np.uint8)
        errors = []

        def receive():
            try:
                camera._update_frame()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=receive)
        thread.start()
        self.addCleanup(thread.join, 2.5)
        self.assertTrue(self.send_eye(camera, 0, noise, count=1, drop=True))
        self.send_eye(camera, 1, noise, count=1)
        time.sleep(0.4)
        self.send_eye(camera, 0, noise)
        self.send_eye(camera, 1, noise)
        thread.join(2.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(camera.wait_until_ready(0))

    def test_timeout_and_restart_release_both_ports(self):
        camera = self.make_camera(receive_timeout=0.08, max_pair_delay=0.02)
        ports = [sock.getsockname()[1] for sock in camera._sockets]
        with self.assertRaisesRegex(TimeoutError, "no fresh pair"):
            camera._update_frame()
        camera.release()
        replacement = self.make_camera(left_port=ports[0], right_port=ports[1])
        color = np.full((64, 80, 3), 100, dtype=np.uint8)
        self.send_eye(replacement, 0, color)
        self.send_eye(replacement, 1, color)
        replacement._update_frame()
        self.assertTrue(replacement.wait_until_ready(0))

    def test_timeout_starts_when_capture_starts(self):
        camera = self.make_camera(receive_timeout=0.08, max_pair_delay=0.02)
        # Other cameras can spend seconds initializing after this camera binds.
        time.sleep(0.1)
        color = np.full((64, 80, 3), 100, dtype=np.uint8)
        self.send_eye(camera, 0, color)
        self.send_eye(camera, 1, color)
        camera._update_frame()
        self.assertTrue(camera.wait_until_ready(0))

    def test_partial_bind_failure_releases_the_first_socket(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.bind(("127.0.0.1", 0))
        port = first.getsockname()[1]
        first.close()
        with self.assertRaises(OSError):
            self.make_camera(left_port=port, right_port=occupied.getsockname()[1])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", port))

    def test_ssrc_change_keeps_left_and_right_order(self):
        camera = self.make_camera()
        color = np.full((64, 80, 3), 100, dtype=np.uint8)
        self.send_eye(camera, 0, color)
        self.send_eye(camera, 1, color)
        camera._update_frame()
        self.send_eye(camera, 0, np.full_like(color, 200), ssrc=99)
        self.send_eye(camera, 1, np.full_like(color, 50))
        camera._update_frame()
        self.assertGreater(camera.get_bgr_frame()[40, 60].mean(), 180)
        self.assertLess(camera.get_bgr_frame()[40, 180].mean(), 70)

    def send_jpeg_eyes(self, camera, payload_type=26):
        for sock, pattern in zip(camera._sockets, ("white", "black")):
            process = subprocess.Popen(
                ["gst-launch-1.0", "-q", "videotestsrc", "is-live=true",
                 "num-buffers=30", f"pattern={pattern}", "!",
                 "video/x-raw,width=80,height=64,framerate=30/1,format=I420",
                 "!", "jpegenc", "!", "rtpjpegpay", f"pt={payload_type}",
                 "mtu=300", "!", "udpsink", "host=127.0.0.1",
                 f"port={sock.getsockname()[1]}", "sync=false"],
            )
            self.addCleanup(process.wait, 2)
            self.addCleanup(process.terminate)

    def test_jpeg_payload_types_decode_and_preserve_eye_aspect_ratio(self):
        for payload_type in (26, 96):
            with self.subTest(payload_type=payload_type):
                camera = self.make_camera(rtp_encoding="JPEG", fps=30, receive_timeout=2.0)
                self.sender.sendto(b"not RTP", camera._sockets[0].getsockname())
                self.send_jpeg_eyes(camera, payload_type)
                camera._update_frame()
                self.assertTrue(camera.wait_until_ready(0))
                self.assertEqual(camera.get_bgr_frame().shape, (80, 240, 3))
                self.assertGreater(camera.get_bgr_frame()[40, 60].mean(), 230)
                self.assertLess(camera.get_bgr_frame()[40, 180].mean(), 20)
                self.assertFalse(camera.get_bgr_frame()[:, :10].any())
                self.assertFalse(camera.get_bgr_frame()[:, 110:130].any())
                for eye in camera.get_eye_frames():
                    self.assertEqual(eye.shape, (64, 80, 3))
                camera.release()

    def test_jpeg_timeout_recovers_and_release_stops_receivers(self):
        camera = self.make_camera(rtp_encoding="JPEG", receive_timeout=0.1)
        ports = [sock.getsockname()[1] for sock in camera._sockets]
        with self.assertRaisesRegex(TimeoutError, "no fresh pair"):
            camera._update_frame()
        camera._receive_timeout = 2.0
        self.send_jpeg_eyes(camera)
        camera._update_frame()
        camera.release()
        self.assertTrue(all(process.poll() is not None for process in camera._jpeg_processes))
        self.assertTrue(all(process.stdout.closed for process in camera._jpeg_processes))
        replacement = self.make_camera(rtp_encoding="JPEG", left_port=ports[0], right_port=ports[1])
        self.assertEqual([sock.getsockname()[1] for sock in replacement._sockets], ports)

    def test_jpeg_receiver_exit_is_reported(self):
        camera = self.make_camera(rtp_encoding="JPEG")
        camera._jpeg_processes[0].terminate()
        camera._jpeg_processes[0].wait(timeout=2)
        with self.assertRaisesRegex(OSError, "JPEG receiver exited"):
            camera._update_frame()

    def test_jpeg_partial_bind_failure_stops_the_first_receiver(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        processes = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        with patch("teleimager.server.subprocess.Popen", side_effect=start):
            with self.assertRaises(OSError):
                self.make_camera(rtp_encoding="JPEG", right_port=occupied.getsockname()[1])
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_server_selects_jpeg_encoding(self):
        config = {
            "camera": {
                "head_camera": {
                    "type": "r1_stereo",
                    "rtp_encoding": "JPEG",
                    "image_shape": [
                        80,
                        240
                    ],
                    "fps": 30,
                    "enable_zmq": True,
                    "left_port": 0,
                    "right_port": 0
                }
            }
        }
        with patch("teleimager.server.ZMQ_Responser"), \
             patch("teleimager.server.ZMQ_PublisherManager"), \
             patch("teleimager.server.WebRTC_PublisherManager"):
            server = TeleImageServer(config)
            try:
                self.assertEqual(server._cameras["head_camera"]._rtp_encoding, "JPEG")
                self.assertEqual(len(server._cameras["head_camera"]._jpeg_processes), 2)
            finally:
                server._clean_up()

    def test_network_only_server_skips_usb_discovery(self):
        config = {
            "camera": {
                "head_camera": {
                    "type": "r1_stereo",
                    "image_shape": [
                        80,
                        240
                    ],
                    "fps": 10,
                    "enable_zmq": True,
                    "left_port": 0,
                    "right_port": 0
                }
            }
        }
        with patch("teleimager.server.UVCCamera.from_config") as factory, \
             patch("teleimager.server.ZMQ_Responser"), \
             patch("teleimager.server.ZMQ_PublisherManager"), \
             patch("teleimager.server.WebRTC_PublisherManager"):
            server = TeleImageServer(config)
            try:
                factory.assert_not_called()
                self.reload_driver.assert_not_called()
                self.assertIsInstance(server._cameras["head_camera"], R1StereoCamera)
            finally:
                server._clean_up()

    def test_app_eye_survives_missing_peer_and_expires(self):
        camera = self.make_camera(receive_timeout=0.2)
        color = np.full((64, 80, 3), (180, 90, 40), dtype=np.uint8)
        self.send_eye(camera, 0, color)
        with self.assertRaises(TimeoutError):
            camera._update_frame()
        self.assertFalse(camera.wait_until_ready(0))
        # Check the per-eye preview independently of the paired ZMQ frame.
        camera._receive_timeout = 1.0
        left, right = camera.get_eye_frames()
        self.assertIsNotNone(left)
        self.assertIsNone(right)
        self.assertIsNone(camera.get_bgr_frame())
        np.testing.assert_allclose(left[32, 40], color[32, 40], atol=12)
        camera._receive_timeout = 0.1
        self.assertEqual(camera.get_eye_frames(), (None, None))
        # A timeout must allow a later capture call to resume reception.
        camera._receive_timeout = 0.5
        self.send_eye(camera, 0, color)
        self.send_eye(camera, 1, color)
        camera._update_frame()
        self.assertTrue(camera.wait_until_ready(0))

    def test_quad_starts_with_missing_cameras_and_isolates_capture_failure(self):
        config = {
            "app_stream": {
                "enabled": True,
                "layout": "stereo_quad"
            },
            "camera": {
                "right_wrist_camera": {
                    "type": "uvc",
                    "physical_path": "/test/camera",
                    "image_shape": [
                        480,
                        640
                    ]
                }
            }
        }
        with patch("teleimager.server.CameraFinder"), \
             patch("teleimager.server.UVCCamera.from_config", side_effect=OSError("disconnected")), \
             patch("teleimager.h264_tcp_stream.H264TcpStreamServer") as stream, \
             patch("teleimager.server.ZMQ_Responser"), \
             patch("teleimager.server.ZMQ_PublisherManager"), \
             patch("teleimager.server.WebRTC_PublisherManager"):
            server = TeleImageServer(config)
            captured = []
            def receive(frame):
                captured.append(frame)
                server.stop()
            stream.return_value.send_frame.side_effect = receive
            try:
                self.assertIsNone(server._cameras["right_wrist_camera"])
                failed = Mock()
                failed.get_fps.return_value = 30
                failed._update_frame.side_effect = OSError("unplugged")
                server._update_frames("right_wrist_camera", failed)
                self.assertFalse(server._stop_event.is_set())
                server.start()
                self.assertTrue(server._stop_event.wait(1))
                stream.return_value.start.assert_called_once()
                self.assertEqual(len(captured), 1)
                self.assertGreater(captured[0].max(), 0)
            finally:
                server.stop()
                server._clean_up()

    def test_quad_requires_zmq_head_camera_to_initialize(self):
        config = {
            "app_stream": {
                "enabled": True,
                "layout": "stereo_quad"
            },
            "camera": {
                "head_camera": {
                    "type": "uvc",
                    "serial_number": "01.00.00",
                    "image_shape": [
                        480,
                        1280
                    ],
                    "fps": 30,
                    "enable_zmq": True
                }
            }
        }
        for uid in (None, "1:3"):
            with self.subTest(uid=uid), \
                 patch("teleimager.server.UVCCamera.from_config") as factory, \
                 patch("teleimager.h264_tcp_stream.H264TcpStreamServer") as stream, \
                 patch("teleimager.server.ZMQ_Responser") as responder, \
                 patch("teleimager.server.ZMQ_PublisherManager") as zmq_manager, \
                 patch("teleimager.server.WebRTC_PublisherManager"):
                factory.side_effect = OSError("disconnected") if uid else None
                factory.return_value = None

                with self.assertRaisesRegex(RuntimeError, "Required ZMQ head_camera"):
                    TeleImageServer(config)

                stream.return_value.start.assert_not_called()
                stream.return_value.stop.assert_called_once()
                responder.return_value.stop.assert_called_once()
                zmq_manager.get_instance.return_value.close.assert_called_once()

    def test_quad_keeps_missing_app_only_head_camera_optional(self):
        config = {
            "app_stream": {
                "enabled": True,
                "layout": "stereo_quad"
            },
            "camera": {
                "head_camera": {
                    "type": "uvc",
                    "serial_number": "01.00.00",
                    "image_shape": [
                        480,
                        1280
                    ],
                    "enable_zmq": False
                }
            }
        }
        with patch("teleimager.server.UVCCamera.from_config") as factory, \
             patch("teleimager.h264_tcp_stream.H264TcpStreamServer"), \
             patch("teleimager.server.ZMQ_Responser"), \
             patch("teleimager.server.ZMQ_PublisherManager"), \
             patch("teleimager.server.WebRTC_PublisherManager"):
            factory.return_value = None
            server = TeleImageServer(config)
            try:
                self.assertIsNone(server._cameras["head_camera"])
            finally:
                server._clean_up()

    def test_uvc_server_uses_public_camera_factory(self):
        camera_config = {
            "type": "uvc", "physical_path": "/test/camera",
            "image_shape": [480, 640], "fps": 30, "enable_zmq": True,
        }
        with patch("teleimager.server.UVCCamera.from_config") as factory, \
             patch("teleimager.server.ZMQ_Responser"), \
             patch("teleimager.server.ZMQ_PublisherManager"), \
             patch("teleimager.server.WebRTC_PublisherManager"):
            server = TeleImageServer({"camera": {"head_camera": camera_config}})
            try:
                factory.assert_called_once_with("head_camera", camera_config)
                self.reload_driver.assert_called_once_with()
            finally:
                server._clean_up()

    def test_app_bgr_output_does_not_require_webrtc_for_public_backends(self):
        frame = np.full((16, 24, 3), (40, 80, 160), dtype=np.uint8)
        jpeg = cv2.imencode(".jpg", frame)[1].tobytes()
        for backend in (
            UVCCamera, V4L2Camera, GStreamerCamera, RealSenseCamera, IsaacSimCamera,
        ):
            with self.subTest(backend=backend.__name__):
                camera = backend.__new__(backend)
                BaseCamera.__init__(camera, "head_camera", {
                    "image_shape": [16, 24], "enable_zmq": True,
                    "enable_webrtc": False,
                })
                self.assertIsNone(camera.get_bgr_frame())
                camera.enable_bgr_output()
                if backend is UVCCamera:
                    camera.cap = Mock()
                    camera.cap.get_frame_robust.return_value = Mock(
                        bgr=frame, jpeg_buffer=jpeg,
                    )
                elif backend is V4L2Camera:
                    camera.container = Mock()
                    camera._passthrough = True
                    camera._demux = iter((jpeg,))
                elif backend is GStreamerCamera:
                    camera.sink = Mock()
                    camera._pull_jpeg = Mock(return_value=jpeg)
                elif backend is RealSenseCamera:
                    camera._enable_depth = False
                    camera.pipeline = Mock()
                    camera.align = Mock()
                    camera.align.process.return_value.get_color_frame.return_value.get_data.return_value = frame
                else:
                    camera._binocular = False
                    camera._image_source = "head"
                    camera.multi_image_reader = Mock()
                    camera.multi_image_reader.read_single_image.return_value = frame
                camera._update_frame()
                self.assertFalse(camera.enable_webrtc())
                self.assertTrue(camera.wait_until_ready(0))
                self.assertIsNotNone(camera.get_jpeg_bytes())
                np.testing.assert_allclose(camera.get_bgr_frame(), frame, atol=3)

    def test_hardware_capture_keeps_public_unpaced_loop(self):
        with patch("teleimager.server.ZMQ_Responser"), \
             patch("teleimager.server.ZMQ_PublisherManager"), \
             patch("teleimager.server.WebRTC_PublisherManager"):
            server = TeleImageServer({"camera": {}})
            camera = Mock()
            camera._capture_requires_pacing = False
            camera._update_frame.side_effect = server.stop
            try:
                with patch.object(server._stop_event, "wait") as wait:
                    server._update_frames("head_camera", camera)
                wait.assert_not_called()
                camera.get_fps.assert_not_called()
                self.assertIn("head_camera", server._camera_frame_times)
            finally:
                server._clean_up()


if __name__ == "__main__":
    unittest.main()
