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
#
# ------------------------------------------------------------------------------
# NOTICE: This file is modified by Unitree Robotics based on portions of
# the "beavr-bot" project (https://github.com/ARCLab-MIT/beavr-bot),
# which is licensed under the MIT License.
# ------------------------------------------------------------------------------


"""Exchange server camera settings over ZMQ and cache successful responses."""

from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import os
import threading

import logging_mp
import yaml
import zmq

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "teleimager"
CLIENT_CONFIG_PATH = str(CONFIG_DIR / "teleimager_client.yaml")


class ZMQ_Responser:
    """Serve native camera configuration as JSON through a background REP loop."""

    def __init__(self, server_config, host: str = "0.0.0.0", port: int = 60000):
        """Bind the configuration responder and start its polling thread.

        Args:
            server_config: Full native server mapping returned for each request.
            host: Local hostname or IP address to bind.
            port: Local TCP port to bind.
        """
        self._server_config = server_config
        self._host = host
        self._port = port
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.REP)
        self._socket.bind(f"tcp://{self._host}:{self._port}")
        self._running = True

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger_mp.info(f"[Teleimager] Camera Config Responser initialized at {self._host}:{self._port}")

    def _run(self):
        """Poll for requests and return the server mapping until stopped."""
        poller = zmq.Poller()
        poller.register(self._socket, zmq.POLLIN)
        while self._running:
            try:
                socks = dict(poller.poll(timeout=200))
                if self._socket in socks and socks[self._socket] == zmq.POLLIN:
                    _ = self._socket.recv()
                    self._socket.send_json(self._server_config)
            except zmq.ZMQError as e:
                if not self._running:
                    break
                logger_mp.error(f"[Teleimager] ZMQError in Responser: {e}")
            except Exception as e:
                logger_mp.error(f"[Teleimager] Unexpected error in Responser: {e}")
    # Public API.
    def get_port(self):
        """Return the configured responder port."""
        return self._port

    def stop(self):
        """Stop the polling thread, then close its socket and owned ZMQ context."""
        self._running = False
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            logger_mp.warning("[Teleimager] Responser thread did not stop gracefully")
        try:
            self._socket.close()
            self._context.term()
        except Exception as e:
            logger_mp.warning(f"[Teleimager] Error closing Responser socket: {e}")


class ZMQ_Requester:
    """Request live camera settings, falling back to the last successful local cache."""

    def __init__(self, host: str, port: int):
        """Create an owned ZMQ context and connect the request socket.

        Args:
            host: Server hostname or IP address.
            port: Server configuration request port.
        """
        self._host = host
        self._port = port
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.REQ)
        self._socket.setsockopt(zmq.LINGER, 0)  # do not wait on close
        self._socket.connect(f"tcp://{self._host}:{self._port}")

        self._poller = zmq.Poller()
        self._poller.register(self._socket, zmq.POLLIN)

    @staticmethod
    def _load_local_config():
        """Return the cached camera mapping, raising if no usable cache can be read."""
        camera_config = None
        if Path(CLIENT_CONFIG_PATH).exists():
            try:
                with open(CLIENT_CONFIG_PATH, "r") as f:
                    camera_config = yaml.safe_load(f)
                logger_mp.debug(f"[Teleimager] Loaded camera config from local {CLIENT_CONFIG_PATH}")
            except Exception as e:
                logger_mp.warning(f"[Teleimager] Failed to load local teleimager_client.yaml: {e}")
        else:
            logger_mp.error("[Teleimager] No local camera config cache found. Run 'teleimager-client' once to fetch it from the server.")

        if camera_config is None:
            raise RuntimeError("Failed to get camera configuration.")

        return camera_config["camera"]

    # Public API.
    def request(self) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Fetch live camera settings, then try the local cache on failure.

        Successful responses overwrite the shared client cache file.

        Returns:
            (camera_config, from_server). from_server is True only for a fresh
            response; camera_config is None if both network and cache fail.
        """
        try:
            self._socket.send(b"GET_DATA")
            socks = dict(self._poller.poll(timeout=1000))

            if self._socket in socks and socks[self._socket] == zmq.POLLIN:
                camera_config = self._socket.recv_json()
                if camera_config is not None:
                    logger_mp.info(f"[Teleimager] Received camera config from server {self._host}:{self._port}")
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                    with open(CLIENT_CONFIG_PATH, "w") as f:
                        yaml.safe_dump(camera_config, f, sort_keys=False, allow_unicode=True)
                    logger_mp.info(f"[Teleimager] Saved camera config to local {CLIENT_CONFIG_PATH}")
                    return camera_config["camera"], True
            else:
                logger_mp.warning(f"[Teleimager] Request to {self._host}:{self._port} timed out or no response.")
        except Exception as e:
            logger_mp.error(f"[Teleimager] Unexpected error in Requester: {e}")

        # Network failed: best-effort fall back to the last cached config.
        try:
            return self._load_local_config(), False
        except Exception:
            return None, False

    def close(self):
        """Close the request socket and terminate its owned ZMQ context."""
        try:
            self._socket.close()
            self._context.term()
        except Exception as e:
            logger_mp.warning(f"[Teleimager] Error closing Requester socket: {e}")
