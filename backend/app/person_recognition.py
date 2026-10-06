from __future__ import annotations

import io
import hashlib
import math
import os
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


PersonCategory = Literal["male", "female", "couple", "unknown"]
PERSON_CATEGORIES = frozenset({"male", "female", "couple", "unknown"})
PERSON_RECOGNIZER_VERSION = "5.2"
_CATEGORY_ALIASES: dict[str, PersonCategory] = {
    "male": "male",
    "man": "male",
    "男": "male",
    "female": "female",
    "woman": "female",
    "女": "female",
    "couple": "couple",
    "mixed": "couple",
    "合拍": "couple",
    "unknown": "unknown",
    "未知": "unknown",
}


class ModelIntegrityError(RuntimeError):
    """Raised when pinned model bytes change before their first compilation."""


def read_openvino_ir_model_from_memory(
    core: Any,
    model_path: str | os.PathLike[str],
) -> Any:
    """Read an OpenVINO IR without passing its filesystem path to native code.

    OpenVINO's Windows path overload can fail when any parent directory contains
    non-ASCII characters and may surface the system-code-page error as a UTF-8
    ``UnicodeDecodeError``.  ``Path.read_bytes`` uses Python's Unicode-aware
    Windows file APIs; the bytes/weights overload then avoids the native path
    conversion entirely.  The ``.bin`` payload must never be decoded as text.
    """

    xml_path = Path(model_path)
    weights_path = xml_path.with_suffix(".bin")
    model_bytes = xml_path.read_bytes()
    weights_bytes = weights_path.read_bytes()
    if not model_bytes:
        raise ValueError(f"OpenVINO IR XML is empty: {xml_path.name}")
    if not weights_bytes:
        raise ValueError(f"OpenVINO IR weights are empty: {weights_path.name}")
    return core.read_model(model=model_bytes, weights=weights_bytes)


def normalize_person_category(value: Any) -> PersonCategory:
    """Normalize only the four durable categories accepted by the collector."""

    if value is None:
        return "unknown"
    return _CATEGORY_ALIASES.get(str(value).strip().casefold(), "unknown")


def normalize_person_category_fields(
    profile: Mapping[str, Any] | None,
    screening: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return safe, mutually consistent copies of external recognition fields.

    ``profile.person_category`` and
    ``screening.person_recognition.category`` are two views of the same durable
    value.  Workers and the public result endpoint can supply either view, so a
    missing view is filled from the one that is present.  If both are present but
    normalize to different values, or if an explicitly supplied recognition
    object is malformed, the conservative result is ``unknown``.  This prevents
    unsupported or contradictory labels from crossing the persistence boundary.
    """

    normalized_profile = dict(profile) if isinstance(profile, Mapping) else {}
    normalized_screening = dict(screening) if isinstance(screening, Mapping) else {}

    candidates: list[PersonCategory] = []
    if "person_category" in normalized_profile:
        candidates.append(
            normalize_person_category(normalized_profile.get("person_category"))
        )

    recognition_supplied = "person_recognition" in normalized_screening
    raw_recognition = normalized_screening.get("person_recognition")
    recognition_is_mapping = isinstance(raw_recognition, Mapping)
    normalized_recognition = (
        dict(raw_recognition) if recognition_is_mapping else {}
    )
    if recognition_is_mapping and "category" in normalized_recognition:
        candidates.append(
            normalize_person_category(normalized_recognition.get("category"))
        )
    elif recognition_supplied and not recognition_is_mapping:
        # An explicit scalar/list/null recognition payload is malformed, rather
        # than merely absent.  Do not allow a second field to make it look valid.
        candidates.append("unknown")

    category: PersonCategory = candidates[0] if candidates else "unknown"
    if any(candidate != category for candidate in candidates[1:]):
        category = "unknown"

    normalized_profile["person_category"] = category
    normalized_recognition["category"] = category
    normalized_screening["person_recognition"] = normalized_recognition
    return normalized_profile, normalized_screening


def _normalized_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    return confidence if math.isfinite(confidence) else None


def _normalized_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


@dataclass(frozen=True, slots=True)
class PersonRecognition:
    category: PersonCategory
    checked: bool = True
    source: str = "local_openvino"
    confidence: float | None = None
    reason: str | None = None
    face_count: int | None = None
    raw_face_count: int | None = None
    adult_face_count: int | None = None
    body_count: int | None = None
    largest_face_pixels: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    recognizer_version: str = PERSON_RECOGNIZER_VERSION
    male_exclusion_confirmed: bool = False

    def as_screening(self, *, enabled: bool = True) -> dict[str, Any]:
        confidence = _normalized_confidence(self.confidence)
        payload: dict[str, Any] = {
            "enabled": enabled,
            "checked": bool(self.checked),
            "category": normalize_person_category(self.category),
            "source": str(self.source or "local_openvino"),
            "male_exclusion_confirmed": self.male_exclusion_confirmed is True,
        }
        if confidence is not None:
            payload["confidence"] = round(min(1.0, max(0.0, confidence)), 6)
        if isinstance(self.face_count, int) and not isinstance(self.face_count, bool):
            payload["face_count"] = max(0, self.face_count)
        for key, value in (
            ("raw_face_count", self.raw_face_count),
            ("adult_face_count", self.adult_face_count),
            ("body_count", self.body_count),
            ("largest_face_pixels", self.largest_face_pixels),
            ("image_width", self.image_width),
            ("image_height", self.image_height),
        ):
            if isinstance(value, int) and not isinstance(value, bool):
                payload[key] = max(0, value)
        payload["recognizer_version"] = str(
            self.recognizer_version or PERSON_RECOGNIZER_VERSION
        )
        if self.reason:
            payload["reason"] = str(self.reason)
        return payload


@runtime_checkable
class PersonClassifier(Protocol):
    """Injectable, synchronous local-inference boundary."""

    def classify(
        self,
        avatar_image_bytes: bytes,
    ) -> PersonRecognition | PersonCategory | dict[str, Any]:
        ...


def normalize_recognition(value: Any) -> PersonRecognition:
    """Accept a compact string or structured result from a local classifier."""

    if isinstance(value, PersonRecognition):
        return PersonRecognition(
            category=normalize_person_category(value.category),
            checked=bool(value.checked),
            source=str(value.source or "local_openvino"),
            confidence=_normalized_confidence(value.confidence),
            reason=str(value.reason) if value.reason else None,
            face_count=(
                max(0, value.face_count)
                if isinstance(value.face_count, int)
                and not isinstance(value.face_count, bool)
                else None
            ),
            raw_face_count=value.raw_face_count,
            adult_face_count=value.adult_face_count,
            body_count=value.body_count,
            largest_face_pixels=value.largest_face_pixels,
            image_width=value.image_width,
            image_height=value.image_height,
            recognizer_version=str(
                value.recognizer_version or PERSON_RECOGNIZER_VERSION
            ),
            male_exclusion_confirmed=value.male_exclusion_confirmed is True,
        )
    if isinstance(value, Mapping):
        face_count = value.get("face_count")
        return PersonRecognition(
            category=normalize_person_category(value.get("category")),
            checked=bool(value.get("checked", True)),
            source=str(value.get("source") or "local_openvino"),
            confidence=_normalized_confidence(value.get("confidence")),
            reason=str(value["reason"]) if value.get("reason") else None,
            face_count=(
                max(0, face_count)
                if isinstance(face_count, int) and not isinstance(face_count, bool)
                else None
            ),
            raw_face_count=_normalized_nonnegative_int(value.get("raw_face_count")),
            adult_face_count=_normalized_nonnegative_int(value.get("adult_face_count")),
            body_count=_normalized_nonnegative_int(value.get("body_count")),
            largest_face_pixels=_normalized_nonnegative_int(
                value.get("largest_face_pixels")
            ),
            image_width=_normalized_nonnegative_int(value.get("image_width")),
            image_height=_normalized_nonnegative_int(value.get("image_height")),
            recognizer_version=str(
                value.get("recognizer_version") or PERSON_RECOGNIZER_VERSION
            ),
            male_exclusion_confirmed=value.get("male_exclusion_confirmed") is True,
        )
    return PersonRecognition(category=normalize_person_category(value))


class LocalOpenVinoPersonClassifier:
    """Two-stage, CPU-only OpenVINO person classifier for an avatar image.

    The bundled Open Model Zoo IR models first detect visible faces and then infer
    a gender probability for each reliable face. A picture containing at least one
    reliable male and one reliable female is classified as ``couple``. Logos,
    cartoons, missing/low-confidence faces and malformed images remain ``unknown``.
    No GPT endpoint, remote inference service, ONNX Runtime or OpenCV is used.
    """

    ASSET_DIRECTORY = Path(__file__).resolve().parent / "assets" / "person_classifier"
    DEFAULT_FACE_MODEL = ASSET_DIRECTORY / "face-detection-retail-0004.xml"
    DEFAULT_GENDER_MODEL = ASSET_DIRECTORY / "age-gender-recognition-retail-0013.xml"
    DEFAULT_PERSON_MODEL = ASSET_DIRECTORY / "person-detection-retail-0013.xml"
    DEFAULT_ATTRIBUTE_MODEL = ASSET_DIRECTORY / "person-attributes-recognition-crossroad-0230.xml"
    BUNDLED_SHA384: dict[str, str] = {
        "face-detection-retail-0004.xml": "a7f8d1d41998503c4f3cdd8c12275f04f1736e5142127edcb4c76c3e17188499390574095a5b2a9dd78d3d0f77d02034",
        "face-detection-retail-0004.bin": "394185d3e42c34d7f9d43229ec8f5755c07e19fd6469d23883e71707fdd8eb66d90cbd62248db90ff3ba1c94adac599b",
        "age-gender-recognition-retail-0013.xml": "a3c4c149f644a8cbcf81fb9fe5c9eb2500c5a102c6dea8ed9ef748eb60e07a0205371d5a839f26592848433971ea8547",
        "age-gender-recognition-retail-0013.bin": "667b71d543f455e5683c70de8ae6db303e8c2362b1e25d3b2539a2ce80bb689ccedefb79b2111b0b8cb6dafcbb2cc5b4",
        "person-detection-retail-0013.xml": "99ad3d4580a0123bef05ff77b6f46ccec16de974d1f5699fb94cd842e3242c6aa641f4977f9a5bb2f0fab42fe51cbb63",
        "person-detection-retail-0013.bin": "a67422e3b5ec76057651d2a0237eab862de00e968c7eef1e5f333849ae64f91900bcd30a23e1b7dbaa07313e358759b9",
        "person-attributes-recognition-crossroad-0230.xml": "f19161f53f7c03c8fee3305b2c55d8de3f795b5e0466ea637b0e24c63de76af61ebcff95ea157bf293a9575e4c64c278",
        "person-attributes-recognition-crossroad-0230.bin": "5a8080bf4a15fff1b6f43eb4fce43682346bc0cf6842fa183739b50cc50098e657fd0917a36ed9891309416a56d832f9",
    }
    BUNDLED_SIZE: dict[str, int] = {
        "face-detection-retail-0004.xml": 143827,
        "face-detection-retail-0004.bin": 1176544,
        "age-gender-recognition-retail-0013.xml": 43414,
        "age-gender-recognition-retail-0013.bin": 4276038,
        "person-detection-retail-0013.xml": 571233,
        "person-detection-retail-0013.bin": 1445734,
        "person-attributes-recognition-crossroad-0230.xml": 261058,
        "person-attributes-recognition-crossroad-0230.bin": 1469624,
    }

    def __init__(
        self,
        face_model_path: str | os.PathLike[str] | None = None,
        gender_model_path: str | os.PathLike[str] | None = None,
        person_model_path: str | os.PathLike[str] | None = None,
        attribute_model_path: str | os.PathLike[str] | None = None,
        *,
        face_confidence_threshold: float = 0.55,
        gender_confidence_threshold: float = 0.50,
        adult_age_threshold: float = 18.0,
        minimum_face_pixels: int = 24,
        minimum_source_face_pixels: int = 10,
        small_face_confidence_threshold: float = 0.78,
        weak_face_gender_threshold: float = 0.50,
        maximum_faces: int = 8,
        maximum_image_bytes: int = 5 * 1024 * 1024,
        person_confidence_threshold: float = 0.60,
        male_face_probability_threshold: float = 0.55,
        male_body_probability_threshold: float = 0.55,
    ) -> None:
        configured_face = (
            face_model_path
            or os.environ.get("IGAC_FACE_DETECTION_MODEL")
            or self.DEFAULT_FACE_MODEL
        )
        configured_gender = (
            gender_model_path
            or os.environ.get("IGAC_AGE_GENDER_MODEL")
            or self.DEFAULT_GENDER_MODEL
        )
        self.face_model_path = Path(configured_face).expanduser()
        self.gender_model_path = Path(configured_gender).expanduser()
        custom_model_directory = (
            self.face_model_path.parent
            if face_model_path is not None or gender_model_path is not None
            else self.ASSET_DIRECTORY
        )
        self.person_model_path = Path(
            person_model_path
            or os.environ.get("IGAC_PERSON_DETECTION_MODEL")
            or custom_model_directory / self.DEFAULT_PERSON_MODEL.name
        ).expanduser()
        self.attribute_model_path = Path(
            attribute_model_path
            or os.environ.get("IGAC_PERSON_ATTRIBUTE_MODEL")
            or custom_model_directory / self.DEFAULT_ATTRIBUTE_MODEL.name
        ).expanduser()
        self.person_confidence_threshold = min(1.0, max(0.0, person_confidence_threshold))
        # The collector is intentionally female-oriented. Preserve UNKNOWN for
        # the exact 50%-55% ambiguity band and classify male only when the model's
        # score reaches the configured threshold in both original and mirrored views.
        self.male_face_probability_threshold = min(
            1.0, max(0.50, male_face_probability_threshold)
        )
        self.male_body_probability_threshold = min(
            1.0, max(0.50, male_body_probability_threshold)
        )
        self.face_confidence_threshold = min(
            1.0, max(0.0, face_confidence_threshold)
        )
        self.gender_confidence_threshold = min(
            1.0, max(0.0, gender_confidence_threshold)
        )
        self.adult_age_threshold = max(0.0, adult_age_threshold)
        self.minimum_face_pixels = max(8, minimum_face_pixels)
        self.minimum_source_face_pixels = max(4, minimum_source_face_pixels)
        self.small_face_confidence_threshold = min(
            1.0, max(self.face_confidence_threshold, small_face_confidence_threshold)
        )
        self.weak_face_gender_threshold = min(
            1.0, max(self.gender_confidence_threshold, weak_face_gender_threshold)
        )
        self.maximum_faces = max(1, maximum_faces)
        self.maximum_image_bytes = max(1, maximum_image_bytes)
        self._compiled_models: tuple[Any, Any, Any, Any] | None = None
        self._model_lock = threading.Lock()
        self._model_integrity_status: str | None = None
        self._diagnostics = threading.local()

    @classmethod
    def from_env(cls) -> "LocalOpenVinoPersonClassifier":
        return cls()

    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        self._diagnostics.detection = {}
        self._diagnostics.classification = {}
        if not isinstance(avatar_image_bytes, (bytes, bytearray, memoryview)):
            return self._unknown(False, "avatar_unavailable")
        payload = bytes(avatar_image_bytes)
        if not payload or len(payload) > self.maximum_image_bytes:
            return self._unknown(False, "avatar_unavailable")
        model_status = self._model_status()
        if model_status != "available":
            return self._unknown(False, model_status)
        try:
            image_rgb = self._decode_image(payload)
            compiled_face, compiled_gender, compiled_person, compiled_attribute = self._get_compiled_models()
            faces = self._detect_faces(compiled_face, image_rgb)
            genders = self._classify_faces(compiled_gender, image_rgb, faces)
            detection = getattr(self._diagnostics, "detection", {})
            if not detection:
                detection = {
                    "raw_face_count": len(faces),
                    "accepted_face_count": len(faces),
                    "largest_face_pixels": max(
                        (
                            min(right - left, bottom - top)
                            for left, top, right, bottom, _ in faces
                        ),
                        default=0,
                    ),
                }
            classification = getattr(self._diagnostics, "classification", {})
            width, height = image_rgb.size
            largest_face = int(detection.get("largest_face_pixels") or 0)
            # Fast path: a clear close-up is classified once. Difficult, sufficiently
            # large avatars get one tiled pass so small people in a group are enlarged
            # for the 300x300 detector without slowing every ordinary account.
            if (
                min(width, height) >= 64
                and (not genders or largest_face < int(min(width, height) * 0.45))
                and type(self)._detect_faces is LocalOpenVinoPersonClassifier._detect_faces
            ):
                tiled_faces = self._detect_faces_multiscale(compiled_face, image_rgb, faces)
                if tiled_faces != faces:
                    faces = tiled_faces
                    genders = self._classify_faces(compiled_gender, image_rgb, faces)
                    tiled_detection = getattr(self._diagnostics, "detection", {})
                    detection = self._merge_detection_diagnostics(
                        detection,
                        tiled_detection,
                        faces,
                    )
                    classification = getattr(self._diagnostics, "classification", {})
        except ModelIntegrityError:
            return self._unknown(False, "model_integrity_failed")
        except Exception:
            return self._unknown(False, "local_inference_failed")

        if not genders:
            # Never turn a child-only face result into an adult label. For scenery,
            # full-body and distant-person photos, use the pedestrian model only as
            # a bounded second pass after facial evidence has failed.
            child_only = int(classification.get("child_face_count") or 0) > 0 and int(
                classification.get("adult_face_count") or 0
            ) == 0
            bodies: list[tuple[int, int, int, int, float]] = []
            body_genders: list[tuple[Literal["male", "female"], float]] = []
            if not child_only:
                bodies = self._detect_people(compiled_person, image_rgb)
                body_genders = self._classify_bodies(compiled_attribute, image_rgb, bodies)
            if body_genders:
                body_categories = {category for category, _ in body_genders}
                if len(body_genders) >= 2 and "female" in body_categories:
                    category: PersonCategory = "couple"
                    confidence = max(value for name, value in body_genders if name == "female")
                else:
                    category = "female" if "female" in body_categories else "male"
                    confidence = max(value for name, value in body_genders if name == category)
                if category == "male":
                    category = "unknown"
                return PersonRecognition(
                    category=category,
                    checked=True,
                    source="local_openvino_body_fallback",
                    confidence=confidence,
                    reason=("male_body_without_clear_face" if category == "unknown"
                            else "body_attribute_fallback"),
                    face_count=len(faces),
                    raw_face_count=int(detection.get("raw_face_count") or 0),
                    adult_face_count=int(classification.get("adult_face_count") or 0),
                    body_count=len(bodies),
                    largest_face_pixels=int(detection.get("largest_face_pixels") or 0),
                    image_width=width,
                    image_height=height,
                )
            reason = self._unreliable_reason(detection, classification)
            if not child_only and bodies:
                reason = "body_attribute_unreliable"
            elif not child_only and not bodies and reason == "no_face_detected":
                reason = "no_person_detected"
            return self._unknown(
                True,
                reason,
                face_count=len(faces),
                raw_face_count=int(detection.get("raw_face_count") or 0),
                adult_face_count=int(classification.get("adult_face_count") or 0),
                body_count=len(bodies),
                largest_face_pixels=int(detection.get("largest_face_pixels") or 0),
                image_width=width,
                image_height=height,
            )
        categories = {category for category, _ in genders}
        if len(genders) >= 2 and "female" in categories:
            female_confidence = max(
                confidence for category, confidence in genders if category == "female"
            )
            return PersonRecognition(
                category="couple",
                checked=True,
                source="local_openvino",
                confidence=female_confidence,
                face_count=len(faces),
                raw_face_count=int(detection.get("raw_face_count") or len(faces)),
                adult_face_count=int(classification.get("adult_face_count") or len(genders)),
                largest_face_pixels=int(detection.get("largest_face_pixels") or 0),
                image_width=width,
                image_height=height,
            )
        category: Literal["male", "female"] = (
            "male" if "male" in categories else "female"
        )
        confidence = max(
            value for detected_category, value in genders if detected_category == category
        )
        male_confirmed = bool(
            category == "male"
            and classification.get("male_exclusion_confirmed") is True
            and int(detection.get("raw_face_count") or len(faces)) <= len(faces)
        )
        if category == "male" and not male_confirmed:
            return self._unknown(
                True, "insufficient_avatar_male_evidence", face_count=len(faces),
                raw_face_count=int(detection.get("raw_face_count") or len(faces)),
                adult_face_count=int(classification.get("adult_face_count") or 0),
                largest_face_pixels=int(detection.get("largest_face_pixels") or 0),
                image_width=width, image_height=height,
            )
        return PersonRecognition(
            category=category,
            checked=True,
            source="local_openvino",
            confidence=confidence,
            face_count=len(faces),
            raw_face_count=int(detection.get("raw_face_count") or len(faces)),
            adult_face_count=int(classification.get("adult_face_count") or len(genders)),
            largest_face_pixels=int(detection.get("largest_face_pixels") or 0),
            image_width=width,
            image_height=height,
            male_exclusion_confirmed=male_confirmed,
        )

    @staticmethod
    def _unknown(
        checked: bool,
        reason: str,
        *,
        face_count: int | None = None,
        raw_face_count: int | None = None,
        adult_face_count: int | None = None,
        body_count: int | None = None,
        largest_face_pixels: int | None = None,
        image_width: int | None = None,
        image_height: int | None = None,
    ) -> PersonRecognition:
        return PersonRecognition(
            category="unknown",
            checked=checked,
            source="local_openvino",
            reason=reason,
            face_count=face_count,
            raw_face_count=raw_face_count,
            adult_face_count=adult_face_count,
            body_count=body_count,
            largest_face_pixels=largest_face_pixels,
            image_width=image_width,
            image_height=image_height,
        )

    def warmup(self) -> None:
        """Compile both local models before collection consumes its first account."""

        if self._model_status() != "available":
            raise RuntimeError("person recognition model is unavailable")
        self._get_compiled_models()

    def _model_status(self) -> str:
        paths = self._model_paths()
        if not all(path.is_file() for path in paths):
            return "model_unavailable"
        return self._bundled_integrity_status(paths, refresh=False)

    def _model_paths(self) -> tuple[Path, ...]:
        return (
            self.face_model_path,
            self.face_model_path.with_suffix(".bin"),
            self.gender_model_path,
            self.gender_model_path.with_suffix(".bin"),
            self.person_model_path,
            self.person_model_path.with_suffix(".bin"),
            self.attribute_model_path,
            self.attribute_model_path.with_suffix(".bin"),
        )

    def _bundled_integrity_status(
        self,
        paths: tuple[Path, ...],
        *,
        refresh: bool,
    ) -> str:
        # Environment overrides intentionally support private compatible IR files.
        # The exact packaged default assets, however, are always integrity checked.
        if all(path.parent == self.ASSET_DIRECTORY for path in paths):
            if refresh or self._model_integrity_status is None:
                try:
                    valid = all(
                        path.is_file()
                        and path.stat().st_size == self.BUNDLED_SIZE.get(path.name)
                        and self._sha384(path) == self.BUNDLED_SHA384.get(path.name)
                        for path in paths
                    )
                except OSError:
                    valid = False
                self._model_integrity_status = (
                    "available" if valid else "model_integrity_failed"
                )
            return self._model_integrity_status
        return "available"

    @staticmethod
    def _sha384(path: Path) -> str:
        digest = hashlib.sha384()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _get_compiled_models(self) -> tuple[Any, Any, Any, Any]:
        with self._model_lock:
            if self._compiled_models is None:
                # ``_model_status`` is intentionally cheap after its first success.
                # Recheck the exact pinned bytes inside the single-flight lock before
                # the first read/compile so a file changed in that interval can never
                # become the cached live model. Once compiled, the immutable OpenVINO
                # objects no longer depend on later on-disk changes.
                if self._bundled_integrity_status(
                    self._model_paths(),
                    refresh=True,
                ) != "available":
                    raise ModelIntegrityError(
                        "bundled OpenVINO model size or SHA-384 changed before compilation"
                    )
                # OpenVINO's Windows native loader is not Unicode-path safe in
                # this pinned release.  The bootstrap verifies and preloads the
                # DLL set from an ASCII-only cache before importing the package.
                from .openvino_native_bootstrap import load_openvino

                ov = load_openvino()

                core = ov.Core()
                # Keep the desktop responsive while several browser windows collect
                # concurrently. OpenVINO otherwise may consume every logical CPU for
                # these tiny models. Two threads per inference preserves parallel
                # throughput without starving Electron/BitBrowser/SQLite.
                try:
                    core.set_property(
                        "CPU",
                        {
                            "PERFORMANCE_HINT": "LATENCY",
                            "INFERENCE_NUM_THREADS": 2,
                        },
                    )
                except Exception:
                    # Older compatible runtimes may not expose both hints. Model
                    # compilation remains functional with their safe defaults.
                    pass
                face_model = read_openvino_ir_model_from_memory(
                    core,
                    self.face_model_path,
                )
                gender_model = read_openvino_ir_model_from_memory(
                    core,
                    self.gender_model_path,
                )
                person_model = read_openvino_ir_model_from_memory(core, self.person_model_path)
                attribute_model = read_openvino_ir_model_from_memory(core, self.attribute_model_path)
                self._compiled_models = (
                    core.compile_model(face_model, "CPU"),
                    core.compile_model(gender_model, "CPU"),
                    core.compile_model(person_model, "CPU"),
                    core.compile_model(attribute_model, "CPU"),
                )
            return self._compiled_models

    @staticmethod
    def _decode_image(payload: bytes) -> Any:
        from PIL import Image, ImageOps  # type: ignore[import-not-found]

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as raw_image:
                if raw_image.width < 1 or raw_image.height < 1:
                    raise ValueError("Avatar image has no pixels")
                if raw_image.width * raw_image.height > 16_000_000:
                    raise ValueError("Avatar image dimensions are too large")
                raw_image.load()
                return raw_image.convert("RGB")

    def _detect_faces(
        self,
        compiled_model: Any,
        image_rgb: Any,
    ) -> list[tuple[int, int, int, int, float]]:
        import numpy  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        width, height = image_rgb.size
        resized = image_rgb.resize((300, 300), Image.Resampling.BILINEAR)
        bgr = numpy.asarray(resized, dtype=numpy.float32)[:, :, ::-1].copy()
        tensor = numpy.expand_dims(numpy.transpose(bgr, (2, 0, 1)), axis=0)
        output = self._infer_named_output(compiled_model, tensor, "detection_out")
        rows = numpy.asarray(output, dtype=numpy.float32).reshape(-1, 7)
        detected: list[tuple[int, int, int, int, float]] = []
        raw_face_count = 0
        too_small_count = 0
        low_confidence_count = 0
        largest_face_pixels = 0
        for row in rows:
            image_id, _, confidence, xmin, ymin, xmax, ymax = (
                float(value) for value in row
            )
            if image_id < 0:
                break
            if confidence < self.face_confidence_threshold:
                if confidence > 0:
                    low_confidence_count += 1
                continue
            raw_face_count += 1
            left = max(0, min(width - 1, int(math.floor(xmin * width))))
            top = max(0, min(height - 1, int(math.floor(ymin * height))))
            right = max(left + 1, min(width, int(math.ceil(xmax * width))))
            bottom = max(top + 1, min(height, int(math.ceil(ymax * height))))
            face_pixels = min(right - left, bottom - top)
            largest_face_pixels = max(largest_face_pixels, face_pixels)
            # The detector always receives a 300x300 tensor. Applying a 48px floor
            # to the *source* image incorrectly rejects a clear 20px face in a 64px
            # avatar even though it occupies about 94px in the actual detector input.
            # Keep a small source-information floor, then judge model suitability in
            # the detector's normalized coordinate system.
            detector_face_pixels = (
                face_pixels * 300.0 / max(1, min(width, height))
            )
            if (
                face_pixels < self.minimum_source_face_pixels
                or detector_face_pixels < self.minimum_face_pixels
            ):
                too_small_count += 1
                continue
            # Faces below the age/gender model's 62px comfort zone *after detector
            # normalization* are admitted only with unusually strong face evidence.
            if (
                detector_face_pixels < 62
                and confidence < self.small_face_confidence_threshold
            ):
                too_small_count += 1
                continue
            detected.append((left, top, right, bottom, confidence))
        detected.sort(key=lambda item: item[4], reverse=True)
        accepted = detected[: self.maximum_faces]
        self._diagnostics.detection = {
            "raw_face_count": raw_face_count,
            "accepted_face_count": len(accepted),
            "too_small_count": too_small_count,
            "low_face_confidence_count": low_confidence_count,
            "largest_face_pixels": largest_face_pixels,
        }
        return accepted

    def _detect_faces_multiscale(
        self,
        compiled_model: Any,
        image_rgb: Any,
        initial_faces: list[tuple[int, int, int, int, float]],
    ) -> list[tuple[int, int, int, int, float]]:
        """Run one bounded overlapping-tile pass for difficult avatar images."""

        width, height = image_rgb.size
        tile_width = max(96, int(width * 0.72))
        tile_height = max(96, int(height * 0.72))
        origins = (
            (0, 0),
            (max(0, width - tile_width), 0),
            (0, max(0, height - tile_height)),
            (max(0, width - tile_width), max(0, height - tile_height)),
        )
        combined = list(initial_faces)
        aggregate = dict(getattr(self._diagnostics, "detection", {}))
        for left, top in dict.fromkeys(origins):
            crop = image_rgb.crop((left, top, left + tile_width, top + tile_height))
            local_faces = self._detect_faces(compiled_model, crop)
            local_diagnostics = getattr(self._diagnostics, "detection", {})
            aggregate["raw_face_count"] = int(
                aggregate.get("raw_face_count") or 0
            ) + int(local_diagnostics.get("raw_face_count") or 0)
            aggregate["too_small_count"] = int(
                aggregate.get("too_small_count") or 0
            ) + int(local_diagnostics.get("too_small_count") or 0)
            aggregate["largest_face_pixels"] = max(
                int(aggregate.get("largest_face_pixels") or 0),
                int(local_diagnostics.get("largest_face_pixels") or 0),
            )
            combined.extend(
                (
                    face_left + left,
                    face_top + top,
                    face_right + left,
                    face_bottom + top,
                    confidence,
                )
                for face_left, face_top, face_right, face_bottom, confidence in local_faces
            )
        deduplicated = self._non_maximum_suppression(combined)
        aggregate["accepted_face_count"] = len(deduplicated)
        aggregate["largest_face_pixels"] = max(
            [int(aggregate.get("largest_face_pixels") or 0)]
            + [min(right - left, bottom - top) for left, top, right, bottom, _ in deduplicated]
        )
        self._diagnostics.detection = aggregate
        return deduplicated

    def _non_maximum_suppression(
        self,
        faces: list[tuple[int, int, int, int, float]],
        *,
        overlap_threshold: float = 0.35,
    ) -> list[tuple[int, int, int, int, float]]:
        kept: list[tuple[int, int, int, int, float]] = []
        for candidate in sorted(faces, key=lambda item: item[4], reverse=True):
            if all(self._intersection_over_union(candidate, existing) < overlap_threshold for existing in kept):
                kept.append(candidate)
            if len(kept) >= self.maximum_faces:
                break
        return kept

    @staticmethod
    def _intersection_over_union(
        first: tuple[int, int, int, int, float],
        second: tuple[int, int, int, int, float],
    ) -> float:
        left = max(first[0], second[0])
        top = max(first[1], second[1])
        right = min(first[2], second[2])
        bottom = min(first[3], second[3])
        intersection = max(0, right - left) * max(0, bottom - top)
        if intersection <= 0:
            return 0.0
        first_area = max(1, first[2] - first[0]) * max(1, first[3] - first[1])
        second_area = max(1, second[2] - second[0]) * max(1, second[3] - second[1])
        return intersection / max(1, first_area + second_area - intersection)

    @staticmethod
    def _merge_detection_diagnostics(
        first: Mapping[str, Any],
        second: Mapping[str, Any],
        faces: list[tuple[int, int, int, int, float]],
    ) -> dict[str, int]:
        return {
            "raw_face_count": max(
                int(first.get("raw_face_count") or 0),
                int(second.get("raw_face_count") or 0),
                len(faces),
            ),
            "accepted_face_count": len(faces),
            "too_small_count": max(
                int(first.get("too_small_count") or 0),
                int(second.get("too_small_count") or 0),
            ),
            "low_face_confidence_count": max(
                int(first.get("low_face_confidence_count") or 0),
                int(second.get("low_face_confidence_count") or 0),
            ),
            "largest_face_pixels": max(
                int(first.get("largest_face_pixels") or 0),
                int(second.get("largest_face_pixels") or 0),
                max(
                    (min(right - left, bottom - top) for left, top, right, bottom, _ in faces),
                    default=0,
                ),
            ),
        }

    def _classify_faces(
        self,
        compiled_model: Any,
        image_rgb: Any,
        faces: list[tuple[int, int, int, int, float]],
    ) -> list[tuple[Literal["male", "female"], float]]:
        import numpy  # type: ignore[import-not-found]
        from PIL import Image, ImageOps  # type: ignore[import-not-found]

        genders: list[tuple[Literal["male", "female"], float]] = []
        child_face_count = 0
        adult_face_count = 0
        low_gender_confidence_count = 0
        verified_male_face_count = 0
        for left, top, right, bottom, face_confidence in faces:
            face = image_rgb.crop((left, top, right, bottom)).resize(
                (62, 62), Image.Resampling.BILINEAR
            )
            bgr = numpy.asarray(face, dtype=numpy.float32)[:, :, ::-1].copy()
            tensor = numpy.expand_dims(numpy.transpose(bgr, (2, 0, 1)), axis=0)
            outputs = self._infer_outputs(
                compiled_model,
                tensor,
                ("prob", "age_conv3"),
            )
            probabilities = numpy.asarray(
                outputs["prob"], dtype=numpy.float32
            ).reshape(-1)
            ages = numpy.asarray(
                outputs["age_conv3"], dtype=numpy.float32
            ).reshape(-1)
            if not ages.size:
                continue
            estimated_age = float(ages[0]) * 100.0
            if (
                not math.isfinite(estimated_age)
            ):
                continue
            if estimated_age < self.adult_age_threshold:
                child_face_count += 1
                continue
            adult_face_count += 1
            if probabilities.size < 2:
                continue
            female_probability = float(probabilities[0])
            male_probability = float(probabilities[1])
            if not all(
                math.isfinite(value) and 0.0 <= value <= 1.0
                for value in (female_probability, male_probability)
            ):
                continue
            # A male candidate always needs a second, mirrored view. Averaging
            # could hide a female/ambiguous result behind one overconfident score;
            # require BOTH views to pass and retain their lower score instead.
            if female_probability <= 0.50:
                mirrored = ImageOps.mirror(face)
                mirrored_bgr = numpy.asarray(
                    mirrored, dtype=numpy.float32
                )[:, :, ::-1].copy()
                mirrored_tensor = numpy.expand_dims(
                    numpy.transpose(mirrored_bgr, (2, 0, 1)), axis=0
                )
                mirrored_outputs = self._infer_outputs(
                    compiled_model,
                    mirrored_tensor,
                    ("prob", "age_conv3"),
                )
                mirrored_probabilities = numpy.asarray(
                    mirrored_outputs["prob"], dtype=numpy.float32
                ).reshape(-1)
                mirrored_ages = numpy.asarray(
                    mirrored_outputs["age_conv3"], dtype=numpy.float32
                ).reshape(-1)
                if not (
                    mirrored_probabilities.size >= 2
                    and all(
                        math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0
                        for value in mirrored_probabilities[:2]
                    )
                    and mirrored_ages.size
                    and math.isfinite(float(mirrored_ages[0]))
                    and float(mirrored_ages[0]) * 100.0 >= self.adult_age_threshold
                    and float(mirrored_probabilities[0]) <= 0.50
                    and min(male_probability, float(mirrored_probabilities[1]))
                    >= self.male_face_probability_threshold
                ):
                    low_gender_confidence_count += 1
                    continue
                male_probability = min(male_probability, float(mirrored_probabilities[1]))
            if female_probability > 0.50:
                category: Literal["male", "female"] = "female"
                gender_confidence = female_probability
            elif male_probability >= self.male_face_probability_threshold:
                category = "male"
                gender_confidence = male_probability
                if self._male_face_quality_passes(
                    image_rgb, (left, top, right, bottom, face_confidence)
                ):
                    verified_male_face_count += 1
            else:
                low_gender_confidence_count += 1
                continue
            # Detection confidence already passed its own reliability gate. Keep
            # the category probability here so the downstream fixed >=55% rule is
            # evaluated against the gender model rather than the detector score.
            genders.append((category, gender_confidence))
        self._diagnostics.classification = {
            "adult_face_count": adult_face_count,
            "child_face_count": child_face_count,
            "low_gender_confidence_count": low_gender_confidence_count,
            "male_exclusion_confirmed": bool(faces) and verified_male_face_count == len(faces),
        }
        return genders

    @staticmethod
    def _male_face_quality_passes(image_rgb: Any, face: tuple[int, int, int, int, float]) -> bool:
        """Conservative source-pixel checks; resizing cannot manufacture evidence.

        These are rejection heuristics, not a calibrated pose/occlusion detector.
        The model score is also not a measured real-world accuracy percentage.
        """
        import numpy

        left, top, right, bottom, detection_confidence = face
        if min(right - left, bottom - top) < 32 or not 0.85 <= detection_confidence <= 1.0:
            return False
        gray = numpy.asarray(
            image_rgb.crop((left, top, right, bottom)).convert("L"), dtype=numpy.float32
        )
        if float(gray.std()) < 12.0:
            return False
        edge_energy = (
            float(numpy.square(numpy.diff(gray, axis=0)).mean())
            + float(numpy.square(numpy.diff(gray, axis=1)).mean())
        ) / 2.0
        return math.isfinite(edge_energy) and edge_energy >= 64.0

    def _detect_people(
        self,
        compiled_model: Any,
        image_rgb: Any,
    ) -> list[tuple[int, int, int, int, float]]:
        """Detect sufficiently visible pedestrians for the body-only fallback."""

        import numpy  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        width, height = image_rgb.size
        resized = image_rgb.resize((544, 320), Image.Resampling.BILINEAR)
        bgr = numpy.asarray(resized, dtype=numpy.float32)[:, :, ::-1].copy()
        tensor = numpy.expand_dims(numpy.transpose(bgr, (2, 0, 1)), axis=0)
        output = self._infer_named_output(compiled_model, tensor, "detection_out")
        rows = numpy.asarray(output, dtype=numpy.float32).reshape(-1, 7)
        people: list[tuple[int, int, int, int, float]] = []
        for row in rows:
            image_id, label, confidence, xmin, ymin, xmax, ymax = (
                float(value) for value in row
            )
            if image_id < 0:
                break
            if label not in (0.0, 1.0) or confidence < self.person_confidence_threshold:
                continue
            left = max(0, min(width - 1, int(math.floor(xmin * width))))
            top = max(0, min(height - 1, int(math.floor(ymin * height))))
            right = max(left + 1, min(width, int(math.ceil(xmax * width))))
            bottom = max(top + 1, min(height, int(math.ceil(ymax * height))))
            box_width, box_height = right - left, bottom - top
            # Attribute model is trained on upright, mostly visible pedestrians.
            # Reject tiny/background fragments and extremely horizontal regions.
            detector_width = box_width * 544.0 / max(1, width)
            if detector_width < 80 or box_height < max(24, int(height * 0.45)):
                continue
            if box_height / max(1, box_width) < 1.25:
                continue
            people.append((left, top, right, bottom, confidence))
        people.sort(key=lambda item: item[4], reverse=True)
        return self._non_maximum_suppression(people)[: self.maximum_faces]

    def _classify_bodies(
        self,
        compiled_model: Any,
        image_rgb: Any,
        bodies: list[tuple[int, int, int, int, float]],
    ) -> list[tuple[Literal["male", "female"], float]]:
        import numpy  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        classified: list[tuple[Literal["male", "female"], float]] = []
        for left, top, right, bottom, person_confidence in bodies:
            crop = image_rgb.crop((left, top, right, bottom)).resize(
                (80, 160), Image.Resampling.BILINEAR
            )
            bgr = numpy.asarray(crop, dtype=numpy.float32)[:, :, ::-1].copy()
            tensor = numpy.expand_dims(numpy.transpose(bgr, (2, 0, 1)), axis=0)
            output = self._infer_named_output(compiled_model, tensor, "453")
            attributes = numpy.asarray(output, dtype=numpy.float32).reshape(-1)
            if not attributes.size:
                continue
            male_probability = float(attributes[0])
            if not math.isfinite(male_probability) or not 0.0 <= male_probability <= 1.0:
                continue
            female_probability = 1.0 - male_probability
            # The attribute model is only supporting evidence and is materially less
            # reliable on seated/side/background portraits than the face branch.
            # Preserve the user's >50% female business rule only after evidence has
            # passed this model-specific reliability gate; ambiguous body output must
            # remain unknown so profile-grid consensus can handle it safely.
            if female_probability > 0.50:
                classified.append(("female", min(person_confidence, female_probability)))
            elif male_probability >= self.male_body_probability_threshold:
                classified.append(("male", male_probability))
        return classified

    @staticmethod
    def _unreliable_reason(
        detection: Mapping[str, Any],
        classification: Mapping[str, Any],
    ) -> str:
        raw_faces = int(detection.get("raw_face_count") or 0)
        accepted_faces = int(detection.get("accepted_face_count") or 0)
        if raw_faces == 0:
            return "no_face_detected"
        if accepted_faces == 0 and int(detection.get("too_small_count") or 0) > 0:
            return "face_too_small"
        if int(classification.get("child_face_count") or 0) > 0 and int(
            classification.get("adult_face_count") or 0
        ) == 0:
            return "child_only"
        if int(classification.get("low_gender_confidence_count") or 0) > 0:
            return "gender_low_confidence"
        return "no_reliable_face"

    @classmethod
    def _infer_named_output(
        cls,
        compiled_model: Any,
        tensor: Any,
        preferred_name: str,
    ) -> Any:
        return cls._infer_outputs(
            compiled_model,
            tensor,
            (preferred_name,),
        )[preferred_name]

    @classmethod
    def _infer_outputs(
        cls,
        compiled_model: Any,
        tensor: Any,
        preferred_names: tuple[str, ...],
    ) -> dict[str, Any]:
        request = compiled_model.create_infer_request()
        result = request.infer({compiled_model.input(0): tensor})
        resolved: dict[str, Any] = {}
        for preferred_name in preferred_names:
            output_port = cls._output_port(compiled_model, preferred_name)
            if output_port is not None and output_port in result:
                resolved[preferred_name] = result[output_port]
                continue
            for key, value in result.items():
                if preferred_name in cls._port_names(key):
                    resolved[preferred_name] = value
                    break
            if preferred_name not in resolved:
                raise ValueError(f"OpenVINO output is missing: {preferred_name}")
        return resolved

    @classmethod
    def _output_port(cls, compiled_model: Any, preferred_name: str) -> Any | None:
        for output in compiled_model.outputs:
            if preferred_name in cls._port_names(output):
                return output
        return None

    @staticmethod
    def _port_names(port: Any) -> set[str]:
        try:
            names = set(port.get_names())
        except Exception:
            names = set()
        try:
            names.add(str(port.any_name))
        except Exception:
            pass
        return names


LocalPersonClassifier = LocalOpenVinoPersonClassifier


def exercise_local_openvino_gender_branch(
    compiled_gender_model: Any,
    *,
    face_model_path: str | os.PathLike[str] | None = None,
    gender_model_path: str | os.PathLike[str] | None = None,
) -> tuple[Literal["male", "female"], float]:
    """Run the production gender-output parser without using a face photograph.

    Build smokes already execute both networks with zero tensors. This additional
    branch supplies a deterministic PIL image and a forced, valid face rectangle
    to the real ``_classify_faces`` implementation, proving that the compiled
    gender model exposes and returns the exact ``prob`` and ``age_conv3`` outputs
    consumed during collection.
    """

    from PIL import Image  # type: ignore[import-not-found]

    classifier = LocalOpenVinoPersonClassifier(
        face_model_path,
        gender_model_path,
        gender_confidence_threshold=0.0,
        adult_age_threshold=0.0,
    )
    # This build smoke validates output names/shapes and parser execution with a
    # synthetic zero-input model result. It is not a real-person decision and must
    # not inherit the production male-confidence gate, which can intentionally
    # return UNKNOWN for ambiguous probabilities.
    classifier.male_face_probability_threshold = 0.0
    image_rgb = Image.new("RGB", (96, 96), (127, 127, 127))
    parsed = classifier._classify_faces(
        compiled_gender_model,
        image_rgb,
        [(8, 8, 88, 88, 1.0)],
    )
    if len(parsed) != 1:
        raise ValueError(
            "OpenVINO gender smoke did not parse exactly one forced face output"
        )
    category, confidence = parsed[0]
    if category not in {"male", "female"} or not math.isfinite(confidence):
        raise ValueError("OpenVINO gender smoke returned an invalid parsed result")
    return category, confidence
