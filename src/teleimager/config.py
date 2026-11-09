"""Read capture settings and compose protocol-specific streaming outputs."""

from pathlib import Path

import yaml


def read_config(path: str | Path) -> dict:
    """Read one YAML mapping without named camera profiles.

    Raises:
        TypeError: The YAML root is not a mapping.
        ValueError: The mapping contains the unsupported profiles key.
    """
    path = Path(path).expanduser()
    with path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise TypeError(f"{path}: configuration must be a mapping.")
    if "profiles" in config:
        raise ValueError(f"{path}: named camera profiles are not supported; provide one camera mapping.")
    return config


def apply_streaming_config(server_config: dict, streaming: dict, *, protocols=("zmq", "tcp", "webrtc")) -> dict:
    """Return server settings with the selected streaming outputs applied.

    Camera mappings are copied before modification. Unselected protocols keep
    their settings; selected ZMQ and WebRTC outputs enable only listed topics
    present in the capture mapping. Unknown topics do not create cameras.

    Args:
        server_config: Native server mapping containing camera settings.
        streaming: Explicit ZMQ, TCP, and WebRTC output settings.
        protocols: Outputs to apply; Runtime selects only ZMQ.

    Returns:
        A server mapping with capture parameters preserved.

    Raises:
        TypeError: Output settings have an unsupported value type.
        ValueError: Fields, codecs, ports, or encoder settings are invalid.
    """
    if not isinstance(streaming, dict):
        raise TypeError("streaming must contain an output mapping.")
    unknown = set(streaming) - {"zmq", "tcp", "webrtc"}
    if unknown:
        raise ValueError(f"Unsupported streaming outputs: {sorted(unknown)!r}.")
    config = {**server_config, "camera": {topic: dict(camera) for topic, camera in server_config["camera"].items()}}
    for protocol in protocols:
        if protocol not in streaming:
            continue
        settings = streaming[protocol]
        if not isinstance(settings, dict):
            raise TypeError(f"streaming.{protocol} must contain a settings mapping.")
        enabled = settings.get("enabled", False)
        if type(enabled) is not bool:
            raise TypeError(f"streaming.{protocol}.enabled must be a boolean.")
        if protocol == "tcp":
            allowed = {"enabled", "codec", "control_host", "control_port", "stream_name", "layout", "camera_topics", "width", "height", "fps", "bitrate", "gop_length", "pixel_format", "preset", "tune", "threads", "status_host", "status_port", "status_timeout",
                       "eye_shape", "max_bitrate", "min_bitrate", "vbv_seconds", "intra_refresh", "codecs"}
        else:
            allowed = {"enabled", "codec", "ports"}
            if protocol == "webrtc":
                allowed.update({"bitrate", "gop_length"})
        if set(settings) - allowed:
            raise ValueError(f"streaming.{protocol} has unsupported fields: {sorted(set(settings) - allowed)!r}.")
        if not enabled:
            if protocol == "tcp":
                config["app_stream"] = dict(settings)
            else:
                for camera in config["camera"].values():
                    camera[f"enable_{protocol}"] = False
            continue
        codec = settings.get("codec")
        choices = {"zmq": {"jpeg"}, "tcp": {"h264"}, "webrtc": {"h264", "vp8"}}[protocol]
        if not isinstance(codec, str) or codec not in choices:
            raise ValueError(f"streaming.{protocol}.codec must be one of {sorted(choices)!r}.")
        if protocol == "tcp":
            for field in ("width", "height", "fps", "bitrate", "gop_length", "threads"):
                value = settings.get(field)
                if type(value) is not int or value <= 0:
                    raise ValueError(f"streaming.tcp.{field} must be a positive integer.")
            for field in ("control_host", "stream_name", "pixel_format", "preset", "tune"):
                value = settings.get(field)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"streaming.tcp.{field} must be a non-empty string.")
            port = settings.get("control_port")
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("streaming.tcp.control_port must be in range 1..65535.")
            if settings["width"] % 2 or settings["height"] % 2:
                raise ValueError("streaming.tcp dimensions must be even for H264 output.")
            if settings["pixel_format"] != "yuv420p":
                raise ValueError("streaming.tcp.pixel_format must be 'yuv420p' for the App decoder.")
            _check_app_stream_settings(settings)
            _check_status_settings(settings)
            config["app_stream"] = dict(settings)
            continue
        ports = settings.get("ports")
        if not isinstance(ports, dict):
            raise TypeError(f"streaming.{protocol}.ports must contain a topic-to-port mapping.")
        for camera in config["camera"].values():
            camera[f"enable_{protocol}"] = False
        used_ports = set()
        for topic, port in ports.items():
            if not isinstance(topic, str) or not topic:
                raise ValueError(f"streaming.{protocol}.ports topic names must be non-empty strings.")
            if type(port) is not int:
                raise TypeError(f"streaming.{protocol}.ports[{topic!r}] must be an integer.")
            if not 1 <= port <= 65535:
                raise ValueError(f"streaming.{protocol}.ports[{topic!r}] must be in range 1..65535.")
            if topic not in config["camera"]:
                continue
            if port in used_ports:
                raise ValueError(f"streaming.{protocol} repeats an active camera port {port}.")
            used_ports.add(port)
            camera = config["camera"][topic]
            camera[f"enable_{protocol}"] = True
            camera[f"{protocol}_port"] = port
            if protocol == "webrtc":
                camera["webrtc_codec"] = codec
        if protocol == "webrtc":
            encoder = {field: settings[field] for field in ("bitrate", "gop_length") if field in settings}
            if "bitrate" in encoder and not isinstance(encoder["bitrate"], dict):
                raise TypeError("streaming.webrtc.bitrate must contain encoder settings.")
            if "gop_length" in encoder:
                value = encoder["gop_length"]
                if type(value) is not int or value <= 0:
                    raise ValueError("streaming.webrtc.gop_length must be a positive integer.")
            if encoder:
                config["webrtc"] = encoder
    return config


def _check_app_stream_settings(settings: dict) -> None:
    """Validate the optional version-two App stream fields of streaming.tcp.

    eye_shape is the (height, width) upper limit of one immersive eye in
    16-pixel blocks; max_bitrate is the immersive budget and min_bitrate the
    floor of bitrate adaptation; vbv_seconds sizes the rate-control buffer;
    codecs lists the encoders offered to the App.
    """
    eye_shape = settings.get("eye_shape")
    if eye_shape is not None:
        if (not isinstance(eye_shape, (list, tuple)) or len(eye_shape) != 2
                or any(type(value) is not int or value < 16 or value % 16 for value in eye_shape)):
            raise ValueError("streaming.tcp.eye_shape must be [height, width] in multiples of 16.")
    for field in ("max_bitrate", "min_bitrate"):
        value = settings.get(field)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"streaming.tcp.{field} must be a positive integer.")
    if settings.get("min_bitrate") is not None and settings.get("max_bitrate") is not None:
        if settings["min_bitrate"] > settings["max_bitrate"]:
            raise ValueError("streaming.tcp.min_bitrate must not exceed max_bitrate.")
    vbv_seconds = settings.get("vbv_seconds")
    if vbv_seconds is not None and (type(vbv_seconds) not in (int, float) or not 0 < vbv_seconds <= 2):
        raise ValueError("streaming.tcp.vbv_seconds must be in (0, 2] seconds.")
    intra_refresh = settings.get("intra_refresh")
    if intra_refresh is not None and type(intra_refresh) is not bool:
        raise TypeError("streaming.tcp.intra_refresh must be a boolean.")
    codecs = settings.get("codecs")
    if codecs is not None:
        if not isinstance(codecs, (list, tuple)) or not codecs or any(codec != "h264" for codec in codecs):
            raise ValueError("streaming.tcp.codecs must list encoders from ['h264'].")


def _check_status_settings(settings: dict) -> None:
    """Validate the optional teleop status relay fields of streaming.tcp.

    Raises:
        ValueError: A present status field has an invalid value.
    """
    if "status_host" in settings and (not isinstance(settings["status_host"], str) or not settings["status_host"]):
        raise ValueError("streaming.tcp.status_host must be a non-empty string.")
    port = settings.get("status_port")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("streaming.tcp.status_port must be null or in range 1..65535.")
    if "status_timeout" in settings:
        timeout = settings["status_timeout"]
        if type(timeout) not in (int, float) or not 0 < timeout < float("inf"):
            raise ValueError("streaming.tcp.status_timeout must be a positive number of seconds.")


def read_server_config(path: str | Path, *, streaming_config: str | Path | None = None) -> dict:
    """Resolve capture references and optional streaming output settings.

    Camera YAML references and the server file's streaming reference are
    relative to the server file. The explicit streaming_config path overrides
    that selection and is interpreted relative to the working directory.

    Args:
        path: Server YAML containing inline cameras or relative YAML paths.
        streaming_config: Optional path to a separate output settings file.

    Returns:
        A native server mapping with references and output settings resolved.

    Raises:
        TypeError: A referenced value is not a camera or settings mapping.
        ValueError: References, legacy profile selections, or outputs are invalid.
    """
    path = Path(path).expanduser()
    config = read_config(path)
    if not isinstance(config.get("camera"), dict):
        raise ValueError("TeleImager requires camera topics below 'camera:'.")
    for topic, camera in config["camera"].items():
        if isinstance(camera, str):
            reference = Path(camera)
            if reference.is_absolute() or reference.suffix != ".yaml":
                raise ValueError(f"{path}: camera {topic!r} must reference a relative .yaml file.")
            camera = read_config(path.parent / reference)
        if not isinstance(camera, dict) or "camera" in camera:
            raise TypeError(f"{path}: camera {topic!r} must contain one camera mapping.")
        if {"config", "profile", "profiles"} & camera.keys():
            raise ValueError(f"{path}: camera {topic!r} config/profile/profiles selections are not supported; provide one camera mapping.")
        config["camera"][topic] = camera
    streaming = config.pop("streaming", None)
    if streaming_config is not None:
        streaming = read_config(streaming_config)
    elif isinstance(streaming, str):
        reference = Path(streaming)
        if reference.is_absolute() or reference.suffix != ".yaml":
            raise ValueError(f"{path}: streaming must reference a relative .yaml file.")
        streaming = read_config(path.parent / reference)
    if streaming is not None:
        config = apply_streaming_config(config, streaming)
    return config
