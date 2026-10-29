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
            allowed = {"enabled", "codec", "control_host", "control_port", "stream_name", "layout", "camera_topics", "width", "height", "fps", "bitrate", "gop_length", "pixel_format", "preset", "tune", "threads"}
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
