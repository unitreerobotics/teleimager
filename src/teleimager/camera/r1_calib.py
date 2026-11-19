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
"""R1 head stereo calibration: read the head camera's own parameters and build rectification maps.

The R1 head serves its EEPROM calibration at http://<head>:9080/cam_param.txt (intrinsics + 8-coefficient rational
distortion per eye, left->right R/T). ``load_head_params`` fetches it (falling back to a local cache when the head is
not reachable yet), and an optional stereo override (left->right R/T measured with a chessboard, see ``fit``) replaces
the EEPROM extrinsic, which can be a few degrees off. The override is bound to its head: ``fit`` writes the head's
``head_fingerprint`` (hash of its EEPROM calibration), and a file whose fingerprint does not match the head being read
is ignored with a warning, so a file copied from another robot can never be applied silently.

Command line (run on the computer that receives the head streams):

    python -m teleimager.camera.r1_calib show   [--source URL]           # parsed parameters + rectified intrinsics
    python -m teleimager.camera.r1_calib capture --tag d060              # one raw pair from UDP 5004/5005 (stop the server first)
    python -m teleimager.camera.r1_calib fit [--write r1_stereo.yaml]    # stereoCalibrate over captured chessboard pairs
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import socket
import struct
import time
import urllib.request
from pathlib import Path

import numpy as np

DEFAULT_SOURCE = "http://192.168.123.161:9080/cam_param.txt"
DEFAULT_CACHE = "~/.config/teleimager/r1_cam_param.txt"
DEFAULT_CAPTURE_DIR = "~/.config/teleimager/r1_stereo_capture"
_DIST_KEYS = ("k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")   # OpenCV rational model order


# ------------------------------------------------------------------------------------------------ parameters
def parse_cam_param(text: str) -> dict:
    """Parse the head's cam_param.txt (EepromDrobotHead dump) into left/right intrinsics and the stereo R/T."""
    sections, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("="):
            continue
        if ":" not in line:
            cur = line.split()[0]
            sections[cur] = {}
            continue
        k, v = line.split(":", 1)
        if cur is not None:
            try:
                sections[cur][k.strip()] = float(v)
            except ValueError:
                pass

    def eye(sec):
        s = sections[sec]
        return {"size": [int(s["width"]), int(s["height"])], "fx": s["fx"], "fy": s["fy"], "cx": s["cx"],
                "cy": s["cy"], "d": [s[k] for k in _DIST_KEYS]}

    rt = sections["r_t_info"]
    R = [[rt[f"r{i}{j}"] for j in (1, 2, 3)] for i in (1, 2, 3)]
    return {"left": eye("m_d_info_l"), "right": eye("m_d_info_r"), "R": R, "T": [rt["tx"], rt["ty"], rt["tz"]],
            "source": "eeprom"}


def head_fingerprint(params: dict) -> str:
    """Identifies one head: hash of its EEPROM intrinsics, distortion and R/T (parsed values, so formatting does not
    matter). Each head is calibrated at the factory, so two heads never share it."""
    def rounded(v):
        if isinstance(v, dict):
            return {k: rounded(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [rounded(x) for x in v]
        return round(float(v), 6)

    canonical = json.dumps(rounded({k: params[k] for k in ("left", "right", "R", "T")}), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def fetch_cam_param(source: str, timeout: float = 3.0) -> str:
    if source.startswith(("http://", "https://")):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # robot LAN: never via a proxy
        with opener.open(source, timeout=timeout) as r:
            return r.read().decode("utf-8")
    return Path(os.path.expanduser(source)).read_text()


def load_head_params(cfg: dict, log=print) -> dict:
    """cfg keys: source (URL or file, default the head's web service), cache (file), wait_s (keep retrying the source
    this long at startup, the head may boot later), stereo (file or {R, T}: left->right extrinsic override)."""
    source = cfg.get("source", DEFAULT_SOURCE)
    cache = Path(os.path.expanduser(cfg.get("cache", DEFAULT_CACHE)))
    deadline = time.monotonic() + float(cfg.get("wait_s", 20.0))
    text, origin, last_err = None, None, None
    while text is None:
        try:
            text = fetch_cam_param(source)
            parse_cam_param(text)
            origin = source
            cache.parent.mkdir(parents=True, exist_ok=True)
            if not cache.exists() or cache.read_text() != text:
                cache.write_text(text)
        except Exception as e:                     # noqa: BLE001 - any failure falls back to the cache
            text, last_err = None, e
            if time.monotonic() > deadline:
                break
            time.sleep(1.0)
    if text is None:
        if not cache.exists():
            raise RuntimeError(f"R1 head calibration: {source} unreachable ({last_err}) and no cache at {cache}")
        text, origin = cache.read_text(), str(cache)
        log(f"R1 head calibration: {source} unreachable ({last_err}); using the cached copy {cache}")
    params = parse_cam_param(text)
    params["origin"] = origin
    params["head_fingerprint"] = head_fingerprint(params)
    stereo = cfg.get("stereo")
    if isinstance(stereo, str):
        p = Path(os.path.expanduser(stereo))
        if p.exists():
            stereo = _read_yaml_or_json(p)
            stereo.setdefault("origin", str(p))
        else:
            log(f"R1 head calibration: stereo override {p} not found; using the EEPROM R/T")
            stereo = None
    if stereo:
        bound = stereo.get("head_fingerprint")
        if bound is None:
            log(f"R1 head calibration: stereo override {stereo.get('origin', 'inline')} has no head_fingerprint, so it "
                f"cannot be checked against this head ({params['head_fingerprint']}); using it anyway. Add "
                f"head_fingerprint: {params['head_fingerprint']} if it was measured on this head, else re-run fit.")
        elif str(bound) != params["head_fingerprint"]:
            log(f"R1 head calibration: stereo override {stereo.get('origin', 'inline')} was measured on head {bound}, "
                f"this head is {params['head_fingerprint']}; IGNORED, using the EEPROM R/T. Re-run "
                f"`python -m teleimager.camera.r1_calib capture/fit` on this robot.")
            stereo = None
    if stereo:
        params["R"], params["T"] = stereo["R"], stereo["T"]
        params["source"] = f"eeprom intrinsics + stereo override ({stereo.get('origin', 'inline')})"
    return params


def _read_yaml_or_json(p: Path) -> dict:
    if p.suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(p.read_text())
    return json.loads(p.read_text())


def K(e) -> np.ndarray:
    return np.array([[e["fx"], 0, e["cx"]], [0, e["fy"], e["cy"]], [0, 0, 1.0]])


def rectify_maps(params: dict, out_size, alpha: float = 0.0, right_dy: float = 0.0):
    """Stereo rectification maps (fixed-point, for cv2.remap) for both eyes and the rectified pinhole parameters."""
    import cv2
    L, Rr = params["left"], params["right"]
    R1, R2, P1, P2, *_ = cv2.stereoRectify(
        K(L), np.array(L["d"], float), K(Rr), np.array(Rr["d"], float), tuple(L["size"]),
        np.array(params["R"], float), np.array(params["T"], float).reshape(3, 1),
        flags=cv2.CALIB_ZERO_DISPARITY, alpha=float(alpha), newImageSize=tuple(out_size))
    P2 = P2.copy()
    P2[1, 2] += float(right_dy)
    maps = []
    for e, Ri, Pi in ((L, R1, P1), (Rr, R2, P2)):
        m = cv2.initUndistortRectifyMap(K(e), np.array(e["d"], float), Ri, Pi, tuple(out_size), cv2.CV_32FC1)
        maps.append(cv2.convertMaps(m[0], m[1], cv2.CV_16SC2))
    info = {"width": int(out_size[0]), "height": int(out_size[1]), "fx": float(P1[0, 0]), "fy": float(P1[1, 1]),
            "cx": float(P1[0, 2]), "cy": float(P1[1, 2]), "baseline_m": float(-P2[0, 3] / P2[0, 0]),
            "R_left_raw_to_rect": R1.tolist(), "source": params.get("source"), "origin": params.get("origin")}
    return maps, info


# ------------------------------------------------------------------------------------------------ raw capture
def reassemble_rtp_jpeg(datagram: bytes, state: dict) -> bytes | None:
    """Feed one RTP/JPEG datagram (RFC 2435 header + R1's complete JPEG file); returns a whole JPEG when the frame's
    marker arrives and all fragments are contiguous, else None. state: {"ts": None, "parts": {}} per stream."""
    if len(datagram) < 20:
        return None
    cc, marker = datagram[0] & 15, datagram[1] >> 7
    ts = struct.unpack(">I", datagram[4:8])[0]
    p = datagram[12 + 4 * cc:]
    off = int.from_bytes(p[1:4], "big")
    typ, q = p[4], p[5]
    hdr = 8 + (4 if typ >= 64 else 0)
    if q >= 128 and off == 0:
        hdr += 4 + struct.unpack(">H", p[hdr + 2:hdr + 4])[0]
    if ts != state.get("ts"):
        state["ts"], state["parts"] = ts, {}
    state["parts"][off] = p[hdr:]
    if not marker:
        return None
    parts, state["ts"], state["parts"] = state["parts"], None, {}
    pos, chunks = 0, []
    for k in sorted(parts):
        if k != pos:
            return None
        chunks.append(parts[k])
        pos += len(parts[k])
    jpeg = b"".join(chunks)
    return jpeg if jpeg[:2] == b"\xff\xd8" else None


def capture_raw_pair(ports=(5004, 5005), source_host="192.168.123.161", timeout=5.0) -> list:
    socks, states, got = [], [], [None, None]
    try:
        for port in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            s.bind(("0.0.0.0", port))
            s.settimeout(0.2)
            socks.append(s)
            states.append({"ts": None, "parts": {}})
        t0 = time.monotonic()
        while None in got and time.monotonic() - t0 < timeout:
            for i, s in enumerate(socks):
                if got[i] is not None:
                    continue
                try:
                    d, addr = s.recvfrom(65535)
                except TimeoutError:
                    continue
                if addr[0] == source_host:
                    got[i] = reassemble_rtp_jpeg(d, states[i])
    finally:
        for s in socks:
            s.close()
    if None in got:
        raise RuntimeError(f"no complete frame on ports {ports} within {timeout}s (server still running?)")
    return got


# ------------------------------------------------------------------------------------------------ CLI
def _board_corners(img, pattern):
    import cv2
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ok, c = cv2.findChessboardCornersSB(g, pattern, cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY)
    return c.reshape(-1, 2).astype(np.float32) if ok else None


def main(argv=None):
    import cv2
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("show", "capture", "fit"))
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--stereo", default=None, help="stereo override file to apply (show)")
    ap.add_argument("--dir", default=DEFAULT_CAPTURE_DIR)
    ap.add_argument("--tag", default=time.strftime("%H%M%S"))
    ap.add_argument("--pattern", default="8x11", help="chessboard INNER corners, e.g. 8x11")
    ap.add_argument("--square", type=float, default=0.03, help="square edge (m)")
    ap.add_argument("--write", default=None, help="fit: write the R/T override here (yaml)")
    ap.add_argument("--size", default="544x448", help="rectified eye size")
    a = ap.parse_args(argv)
    pattern = tuple(int(v) for v in a.pattern.split("x"))
    out = tuple(int(v) for v in a.size.split("x"))
    d = Path(os.path.expanduser(a.dir))
    params = load_head_params({"source": a.source, "wait_s": 3, "stereo": a.stereo}, log=print)
    if a.cmd == "show":
        print(json.dumps({k: params[k] for k in ("left", "right", "R", "T", "source", "origin", "head_fingerprint")},
                         indent=1))
        print(json.dumps(rectify_maps(params, out)[1], indent=1))
        return
    if a.cmd == "capture":
        d.mkdir(parents=True, exist_ok=True)
        for jpeg, eye in zip(capture_raw_pair(), ("left", "right")):
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            (d / f"{a.tag}_{eye}.jpg").write_bytes(jpeg)
            c = _board_corners(img, pattern)
            print(f"{a.tag} {eye}: {img.shape[1]}x{img.shape[0]}, board "
                  f"{f'found, width {np.ptp(c[:, 0]):.0f} px' if c is not None else 'NOT found'}")
        return
    obj = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    obj[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2) * a.square
    views = []
    for f in sorted(glob.glob(str(d / "*_left.jpg"))):
        cl = _board_corners(cv2.imread(f), pattern)
        cr = _board_corners(cv2.imread(f.replace("_left.jpg", "_right.jpg")), pattern)
        if cl is not None and cr is not None:
            views.append((Path(f).name[:-9], cl, cr))
    if len(views) < 2:
        raise SystemExit(f"need >= 2 board pairs at different distances in {d} (have {len(views)})")
    L, Rr = params["left"], params["right"]
    rms, *_, R, T, _, _ = cv2.stereoCalibrate(
        [obj] * len(views), [v[1] for v in views], [v[2] for v in views], K(L), np.array(L["d"]), K(Rr),
        np.array(Rr["d"]), tuple(L["size"]), flags=cv2.CALIB_FIX_INTRINSIC | cv2.CALIB_RATIONAL_MODEL)
    params["R"], params["T"] = R.tolist(), T.ravel().tolist()
    _, info = rectify_maps(params, out)
    print(f"{len(views)} pairs, stereo rms {rms:.3f} px, baseline {np.linalg.norm(T) * 1000:.1f} mm; "
          f"rectified f {info['fx']:.1f} cx {info['cx']:.1f} cy {info['cy']:.1f}")
    R1, R2, P1, P2, *_ = cv2.stereoRectify(K(L), np.array(L["d"]), K(Rr), np.array(Rr["d"]), tuple(L["size"]), R, T,
                                           flags=cv2.CALIB_ZERO_DISPARITY, alpha=0, newImageSize=out)
    for tag, cl, cr in views:
        ul = cv2.undistortPoints(cl.reshape(-1, 1, 2), K(L), np.array(L["d"]), R=R1, P=P1).reshape(-1, 2)
        ur = cv2.undistortPoints(cr.reshape(-1, 1, 2), K(Rr), np.array(Rr["d"]), R=R2, P=P2).reshape(-1, 2)
        disp = (ul[:, 0] - ur[:, 0]).mean()
        print(f"  {tag}: row offset {(ur[:, 1] - ul[:, 1]).mean():+.2f} px, stereo depth "
              f"{info['fx'] * info['baseline_m'] / disp:.3f} m")
    if a.write:
        import yaml
        Path(os.path.expanduser(a.write)).write_text(yaml.safe_dump({
            "head_fingerprint": params["head_fingerprint"],
            "R": [[round(v, 7) for v in r] for r in R.tolist()], "T": [round(v, 7) for v in T.ravel().tolist()],
            "rms_px": round(float(rms), 4), "views": [v[0] for v in views], "pattern": list(pattern),
            "square_m": a.square, "date": time.strftime("%Y-%m-%d %H:%M")}, sort_keys=False))
        print("written", a.write)


if __name__ == "__main__":
    main()
