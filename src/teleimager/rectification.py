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

"""Rectified head-camera eyes for the App's immersive view, from a unit's calibration.

The server rectifies a head topic whose settings set ``rectify_immersive:
true``: a source that already delivers rectified eyes leaves it out. The
topic's ``calibration`` mapping holds the measured optics of one
camera device: ``views`` with ``mono``, or ``left`` and ``right``, each with
``image_width``, ``image_height``, ``distortion_model`` (the ROS
``CameraInfo`` name ``plumb_bob``, ``rational_polynomial`` or
``equidistant``), ``camera_matrix`` and ``distortion_coefficients`` in OpenCV
order; two eyes add ``T_right_from_left``, the 4x4 transform with
``p_right = R @ p_left + t`` between optical frames (OpenCV
``stereoCalibrate`` ``R, T``). Robotree writes it from the robot's
calibration file.

A rectified eye is a pinhole view with square pixels, its principal point at
the image center and ``eye_fov`` degrees from its left edge to its right edge.
The two eyes of one device are turned onto their common baseline
(``cv2.stereoRectify``) and share that view, so their rows match and objects
at infinity land on the same pixel in both eyes, which is what the App's
immersive view expects. Every robot uses the same ``eye_fov``, so the App
shows every rectified head camera at one angular scale. Rays outside the
camera's image stay black.

A frame handed to a rectifier is the calibrated image, scaled: its width over
height must equal the calibration's. A frame of another shape, such as a
cropped camera mode, is fitted unrectified, with one warning.
"""

from __future__ import annotations

import math

import cv2
import logging_mp
import numpy as np

from .frame import fit_frame

logger_mp = logging_mp.getLogger(__name__)
logger_mp.setLevel(logging_mp.INFO)

# ROS CameraInfo model names with their OpenCV coefficient counts.
DISTORTION_COEFFICIENT_COUNTS = {"plumb_bob": 5, "rational_polynomial": 8, "equidistant": 4}
# Wide enough to keep the robot's hands in view on the widest head cameras
# (R1 hands sit about 45 degrees below the optical axis), and the App's 70
# degree quad then shows about the scale the unrectified view had.
DEFAULT_EYE_FOV = 110.0  # degrees
# Width over height of a frame may differ from the calibration's by rounding.
_ASPECT_TOLERANCE = 0.01
# Ray angles at which a distortion model is checked to keep growing outward.
_FOLD_CHECK_ANGLES = np.radians(np.arange(0.0, 89.75, 0.25))


class Rectifier:
    """Rectified display frames of one camera topic: one eye, or two side by side.

    Attributes:
        eyes: ``EyeRectifier`` of each part of a frame, left to right.
        label: Name of the camera topic in log lines and errors.
    """

    def __init__(self, eyes, *, label):
        self.eyes = tuple(eyes)
        self.label = label
        self._mismatch_warned = False

    @classmethod
    def from_calibration(cls, calibration, *, eyes, fov=DEFAULT_EYE_FOV, label="calibration"):
        """Build the rectifier of the calibration views a topic's frames show.

        Args:
            calibration: The topic's ``calibration`` mapping (module docstring).
            eyes: Views the frame shows, left to right: ``("mono",)``,
                ``("left",)``, ``("right",)`` or ``("left", "right")``.
            fov: Horizontal field of view of each rectified eye in degrees.
            label: Name of the camera topic in errors and log lines.

        Raises:
            TypeError: If the calibration or one of its views is not a mapping.
            ValueError: If the views are not ``mono`` or ``left`` and
                ``right``, lack an eye the frame shows, or hold invalid values.
        """
        if not isinstance(calibration, dict) or not isinstance(calibration.get("views"), dict):
            raise TypeError(f"{label}.calibration must be a mapping with a views mapping.")
        if not 0 < fov < 180:
            raise ValueError(f"{label}: eye_fov must be between 0 and 180 degrees, got {fov!r}.")
        views = calibration["views"]
        if set(views) not in ({"mono"}, {"left", "right"}):
            raise ValueError(f"{label}.calibration.views must be mono, or left and right, got {sorted(views)!r}.")
        missing = [eye for eye in eyes if eye not in views]
        if missing:
            raise ValueError(f"{label}.calibration has views {sorted(views)!r}, but the stream shows {missing!r}.")
        if set(views) == {"mono"}:
            rotations = {"mono": np.eye(3)}
        else:
            rotations = _stereo_rotations(calibration.get("T_right_from_left"), label=f"{label}.calibration")
        return cls(
            [EyeRectifier(views[eye], rotation=rotations[eye], fov=fov, label=f"{label}.calibration.views.{eye}")
             for eye in eyes],
            label=label,
        )

    def render(self, frame, shape):
        """Return frame rectified to shape (height, width), each eye in its share of the width.

        A frame whose eyes do not have the calibrated width over height is
        fitted unrectified instead, and the first such frame logs a warning.
        """
        height, width = shape
        parts = np.hsplit(frame, len(self.eyes)) if len(self.eyes) > 1 else (frame,)
        if not all(eye.matches(part.shape[:2]) for eye, part in zip(self.eyes, parts)):
            if not self._mismatch_warned:
                self._mismatch_warned = True
                calibrated_height, calibrated_width = self.eyes[0].image_size
                logger_mp.warning(
                    "[Rectifier] %s: an eye of %dx%d is not the calibrated %dx%d scaled; "
                    "the immersive view stays unrectified.",
                    self.label, parts[0].shape[1], parts[0].shape[0], calibrated_width, calibrated_height,
                )
            return fit_frame(frame, width, height)
        if len(self.eyes) == 1:
            return self.eyes[0].remap(frame, (height, width))
        part_width = width // len(self.eyes)
        output = np.zeros((height, width, 3), dtype=np.uint8)
        for index, (eye, part) in enumerate(zip(self.eyes, parts)):
            output[:, index * part_width:(index + 1) * part_width] = eye.remap(part, (height, part_width))
        return output


class EyeRectifier:
    """Remap tables from one calibrated eye to its rectified pinhole view.

    Args:
        view: One entry of ``calibration.views`` (module docstring).
        rotation: 3x3 rotation from the eye's optical frame to the rectified
            frame: R1 or R2 of ``cv2.stereoRectify``, identity for one eye.
        fov: Horizontal field of view of the rectified view in degrees.
        label: Name of the view in errors.

    Attributes:
        image_size: (height, width) of the calibrated image in pixels.
        distortion_model: ROS ``CameraInfo`` model name.
        camera_matrix: 3x3 ``K`` of the calibrated image.
        distortion_coefficients: Coefficients in the model's OpenCV order.
        rotation: As the argument.
        fov: As the argument.
        max_ray_angle: Ray angle from the optical axis in radians up to which
            the distortion model maps rays outward; past it the model folds
            back, and those rays stay black instead of showing a mirror image.
    """

    def __init__(self, view, *, rotation, fov, label):
        if not isinstance(view, dict):
            raise TypeError(f"{label} must be a mapping.")
        width, height = view.get("image_width"), view.get("image_height")
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError(f"{label}.image_width and image_height must be positive integers.")
        model = view.get("distortion_model")
        if model not in DISTORTION_COEFFICIENT_COUNTS:
            raise ValueError(f"{label}.distortion_model must be one of {sorted(DISTORTION_COEFFICIENT_COUNTS)!r}, got {model!r}.")
        camera_matrix = np.asarray(view.get("camera_matrix"), dtype=np.float64)
        if camera_matrix.shape != (3, 3) or not np.isfinite(camera_matrix).all():
            raise ValueError(f"{label}.camera_matrix must be a finite 3x3 matrix.")
        coefficients = np.asarray(view.get("distortion_coefficients"), dtype=np.float64)
        if coefficients.shape != (DISTORTION_COEFFICIENT_COUNTS[model],) or not np.isfinite(coefficients).all():
            raise ValueError(f"{label}.distortion_coefficients must hold {DISTORTION_COEFFICIENT_COUNTS[model]} finite values for {model}.")
        self.image_size = (height, width)
        self.distortion_model = model
        self.camera_matrix = camera_matrix
        self.distortion_coefficients = coefficients
        self.rotation = np.asarray(rotation, dtype=np.float64)
        self.fov = float(fov)
        self.max_ray_angle = self._find_max_ray_angle()
        self._maps_key = None
        self._maps = None

    def matches(self, frame_shape):
        """Return whether a frame of (height, width) is the calibrated image, scaled."""
        frame_height, frame_width = frame_shape
        height, width = self.image_size
        return abs(frame_width * height / (frame_height * width) - 1.0) <= _ASPECT_TOLERANCE

    def remap(self, frame, shape):
        """Return frame, the calibrated image scaled, as the rectified view of shape (height, width)."""
        key = (frame.shape[:2], tuple(shape))
        if key != self._maps_key:
            self._maps = self.build_maps(frame.shape[:2], shape)
            self._maps_key = key
        return cv2.remap(frame, self._maps[0], self._maps[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    def build_maps(self, frame_shape, shape):
        """Return fixed-point ``cv2.remap`` tables from a frame of frame_shape to the view of shape.

        Args:
            frame_shape: (height, width) of the frame, the calibrated image scaled.
            shape: (height, width) of the rectified view.
        """
        frame_height, frame_width = frame_shape
        height, width = self.image_size
        scale_x, scale_y = frame_width / width, frame_height / height
        camera_matrix = self.camera_matrix.copy()
        # Pixel centers sit at integer coordinates, so the principal point
        # scales about -0.5.
        camera_matrix[0, :2] *= scale_x
        camera_matrix[0, 2] = (camera_matrix[0, 2] + 0.5) * scale_x - 0.5
        camera_matrix[1, 1] *= scale_y
        camera_matrix[1, 2] = (camera_matrix[1, 2] + 0.5) * scale_y - 0.5
        view_height, view_width = shape
        focal = 0.5 * view_width / math.tan(math.radians(self.fov) / 2)
        center_x, center_y = (view_width - 1) / 2, (view_height - 1) / 2
        projection = np.array([[focal, 0.0, center_x], [0.0, focal, center_y], [0.0, 0.0, 1.0]])
        if self.distortion_model == "equidistant":
            map_x, map_y = cv2.fisheye.initUndistortRectifyMap(
                camera_matrix, self.distortion_coefficients, self.rotation, projection,
                (view_width, view_height), cv2.CV_32FC1,
            )
        else:
            map_x, map_y = cv2.initUndistortRectifyMap(
                camera_matrix, self.distortion_coefficients, self.rotation, projection,
                (view_width, view_height), cv2.CV_32FC1,
            )
        # A view pixel's ray in the eye's optical frame is rotation.T @ (x, y, 1).
        x, y = np.meshgrid((np.arange(view_width) - center_x) / focal, (np.arange(view_height) - center_y) / focal)
        rays = np.stack([x, y, np.ones_like(x)], axis=-1) @ self.rotation
        angles = np.arctan2(np.hypot(rays[..., 0], rays[..., 1]), rays[..., 2])
        folded = angles >= self.max_ray_angle
        map_x[folded] = -1.0
        map_y[folded] = -1.0
        return cv2.convertMaps(map_x, map_y, cv2.CV_16SC2)

    def _find_max_ray_angle(self):
        """Return the ray angle in radians at which the model's image radius stops growing."""
        points = np.stack(
            [np.tan(_FOLD_CHECK_ANGLES), np.zeros_like(_FOLD_CHECK_ANGLES), np.ones_like(_FOLD_CHECK_ANGLES)], axis=1,
        )
        zero = np.zeros(3)
        if self.distortion_model == "equidistant":
            projected, _ = cv2.fisheye.projectPoints(
                points[None], zero, zero, self.camera_matrix, self.distortion_coefficients,
            )
        else:
            projected, _ = cv2.projectPoints(points, zero, zero, self.camera_matrix, self.distortion_coefficients)
        radius = projected.reshape(-1, 2)[:, 0] - self.camera_matrix[0, 2]
        shrinking = np.flatnonzero(np.diff(radius) <= 0)
        return float(_FOLD_CHECK_ANGLES[shrinking[0]]) if shrinking.size else math.pi / 2


def _stereo_rotations(transform, *, label):
    """Return the rotations that turn each eye of a stereo pair onto the common baseline.

    Raises:
        ValueError: If transform is not a 4x4 matrix with a rotation and a
            translation of non-zero length.
    """
    transform = np.asarray(transform if transform is not None else (), dtype=np.float64)
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        raise ValueError(f"{label}.T_right_from_left must be a finite 4x4 matrix for left and right views.")
    rotation, translation = transform[:3, :3], transform[:3, 3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4) or np.linalg.norm(translation) == 0:
        raise ValueError(f"{label}.T_right_from_left must hold a rotation and a non-zero baseline.")
    # R1 and R2 depend only on the transform, so any intrinsics and size do.
    left_rotation, right_rotation, *_ = cv2.stereoRectify(
        np.eye(3), None, np.eye(3), None, (2, 2), rotation, translation, flags=cv2.CALIB_ZERO_DISPARITY,
    )
    return {"left": left_rotation, "right": right_rotation}
