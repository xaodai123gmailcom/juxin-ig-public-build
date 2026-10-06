from __future__ import annotations

import asyncio
import io
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.execution_manager import ExecutionManager
from app.database import Database
from app.person_recognition import (
    LocalOpenVinoPersonClassifier,
    PERSON_RECOGNIZER_VERSION,
    PersonRecognition,
    exercise_local_openvino_gender_branch,
    normalize_person_category,
    normalize_recognition,
    read_openvino_ir_model_from_memory,
)
from app.playwright_worker import (
    PlaywrightWorker,
    VisibleProfile,
    extract_embedded_profile_evidence,
)
from app.schemas import DesktopTaskCreateRequest, ResultRequest, TaskSettingsRequest
from app.service import CoreService, validate_task_settings


def _execution_control() -> SimpleNamespace:
    pause_event = asyncio.Event()
    pause_event.set()
    return SimpleNamespace(
        owner_user_id="owner",
        task_id="task",
        pause_event=pause_event,
        stop_event=asyncio.Event(),
    )


class _RecordingService:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def record_result(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        **payload: Any,
    ) -> dict[str, Any]:
        self.results.append(
            {
                "owner_user_id": owner_user_id,
                "task_id": task_id,
                "target_id": target_id,
                **payload,
            }
        )
        return {"deduped": False}


class _StableIdentityDuplicateService(_RecordingService):
    def __init__(self) -> None:
        super().__init__()
        self.confirmations: list[dict[str, Any]] = []

    def confirm_workbench_identity(self, owner_user_id: str, **payload: Any) -> dict[str, Any]:
        self.confirmations.append({"owner_user_id": owner_user_id, **payload})
        return {"duplicate": True, "should_continue": False}


class _ProfileWorker:
    supports_avatar_image_capture = True

    def __init__(self) -> None:
        self.avatar_capture_requests: list[bool] = []
        self.profile_reads: list[str] = []

    async def read_visible_profile(
        self,
        username: str,
        *,
        include_activity: bool = False,
        include_avatar_image: bool = False,
    ) -> dict[str, Any]:
        del include_activity
        self.profile_reads.append(username)
        self.avatar_capture_requests.append(include_avatar_image)
        return {
            "username": username,
            "visibility": "public",
            "followers": 100,
            "following": 50,
            "posts": 10,
            "avatar_url": "https://example.invalid/avatar.jpg",
            **(
                {"avatar_image_bytes": b"rendered-avatar-png"}
                if include_avatar_image
                else {}
            ),
        }

    async def read_visible_account_location(self, username: str) -> str:
        del username
        return "美国"


class _NonUsProfileWorker(_ProfileWorker):
    async def read_visible_account_location(self, username: str) -> str:
        del username
        return "加拿大"


class _DeferredNonUsProfileWorker(_NonUsProfileWorker):
    def __init__(self) -> None:
        super().__init__()
        self.deferred_avatar_requests = 0

    async def capture_visible_review_snapshot(
        self, username: str, *, include_post_previews: bool
    ) -> dict[str, Any]:
        del username, include_post_previews
        self.deferred_avatar_requests += 1
        return {"avatar_image_bytes": b"must-not-be-read"}


class _StableIdProfileWorker(_ProfileWorker):
    async def read_visible_profile(
        self,
        username: str,
        *,
        include_activity: bool = False,
        include_avatar_image: bool = False,
    ) -> dict[str, Any]:
        profile = await super().read_visible_profile(
            username,
            include_activity=include_activity,
            include_avatar_image=include_avatar_image,
        )
        profile["instagram_user_id"] = "123456789012345"
        return profile


class _StaticClassifier:
    def __init__(self, category: str) -> None:
        self.category = category
        self.images: list[bytes] = []

    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        self.images.append(bytes(avatar_image_bytes))
        return PersonRecognition(
            category=normalize_person_category(self.category),
            source="test_local_model",
            confidence=0.98,
        )


class _ExplodingClassifier:
    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        del avatar_image_bytes
        raise RuntimeError("simulated local model failure")


class _InvalidConfidenceClassifier:
    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        del avatar_image_bytes
        return PersonRecognition(
            category="female",
            confidence="not-a-number",  # type: ignore[arg-type]
        )


class _BlockingClassifier:
    def __init__(self, category: str = "female") -> None:
        self.category = category
        self.started = threading.Event()
        self.release = threading.Event()

    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        del avatar_image_bytes
        self.started.set()
        self.release.wait(timeout=5)
        return PersonRecognition(category=normalize_person_category(self.category))


class _ConcurrencyClassifier:
    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()

    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        del avatar_image_bytes
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            time.sleep(self.delay)
            return PersonRecognition(category="female")
        finally:
            with self.lock:
                self.active -= 1


class _TwoSlotBlockingClassifier:
    def __init__(self) -> None:
        self.active = 0
        self.lock = threading.Lock()
        self.two_started = threading.Event()
        self.release = threading.Event()

    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        del avatar_image_bytes
        with self.lock:
            self.active += 1
            if self.active >= 2:
                self.two_started.set()
        try:
            self.release.wait(timeout=5)
            return PersonRecognition(category="female")
        finally:
            with self.lock:
                self.active -= 1


class _EventClassifier:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def classify(self, avatar_image_bytes: bytes) -> PersonRecognition:
        del avatar_image_bytes
        self.events.append("person")
        return PersonRecognition(category="male")


class _EventReviewer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def review(self, profile: dict[str, Any]) -> dict[str, Any]:
        del profile
        self.events.append("gpt")
        return {"review_status": "reviewed"}


class _ModelLogicClassifier(LocalOpenVinoPersonClassifier):
    def __init__(self, faces: list[tuple[str, float, float]]) -> None:
        super().__init__()
        self.test_faces = faces
        self.test_outputs = [item for item in faces for _ in range(2 if item[0] == "male" and item[2] >= 18 else 1)]
        self.inference_index = 0

    def _model_status(self) -> str:
        return "available"

    @staticmethod
    def _decode_image(payload: bytes) -> object:
        del payload
        from PIL import Image

        return Image.new("RGB", (120, 120), "white")

    def _get_compiled_models(self) -> tuple[object, object, object, object]:
        return object(), object(), object(), object()

    def _detect_faces(
        self, compiled_model: object, image_rgb: object
    ) -> list[tuple[int, int, int, int, float]]:
        del compiled_model, image_rgb
        return [(0, 0, 80, 80, 0.95) for _ in self.test_faces]

    def _detect_people(self, compiled_model: object, image_rgb: object) -> list[Any]:
        del compiled_model, image_rgb
        return []

    def _infer_outputs(
        self,
        compiled_model: object,
        tensor: object,
        preferred_names: tuple[str, ...],
    ) -> dict[str, Any]:
        del compiled_model, tensor
        import numpy

        self.assert_output_names(preferred_names)
        category, confidence, age = self.test_outputs[
            min(self.inference_index, len(self.test_outputs) - 1)
        ]
        self.inference_index += 1
        probabilities = (
            [confidence, 1.0 - confidence]
            if category == "female"
            else [1.0 - confidence, confidence]
        )
        return {
            "prob": numpy.asarray(probabilities, dtype=numpy.float32).reshape(1, 2, 1, 1),
            "age_conv3": numpy.asarray([age / 100.0], dtype=numpy.float32).reshape(1, 1, 1, 1),
        }

    @staticmethod
    def assert_output_names(preferred_names: tuple[str, ...]) -> None:
        if preferred_names != ("prob", "age_conv3"):
            raise AssertionError(preferred_names)


class _BodyFallbackLogicClassifier(_ModelLogicClassifier):
    def __init__(self, bodies: list[tuple[str, float]]) -> None:
        super().__init__([])
        self.test_bodies = bodies

    def _detect_people(self, compiled_model: object, image_rgb: object) -> list[Any]:
        del compiled_model, image_rgb
        return [(10, 5, 70, 115, 0.95) for _ in self.test_bodies]

    def _classify_bodies(
        self, compiled_model: object, image_rgb: object, bodies: list[Any]
    ) -> list[Any]:
        del compiled_model, image_rgb, bodies
        return list(self.test_bodies)


class _DetectionOutputClassifier(LocalOpenVinoPersonClassifier):
    def __init__(self, normalized_size: float) -> None:
        super().__init__()
        self.normalized_size = normalized_size

    def _infer_named_output(
        self,
        compiled_model: object,
        tensor: object,
        preferred_name: str,
    ) -> Any:
        del compiled_model, tensor
        import numpy

        if preferred_name != "detection_out":
            raise AssertionError(preferred_name)
        return numpy.asarray(
            [
                [
                    0,
                    1,
                    0.99,
                    0,
                    0,
                    self.normalized_size,
                    self.normalized_size,
                ]
            ],
            dtype=numpy.float32,
        ).reshape(1, 1, 1, 7)


class _FakeAvatarImage:
    def __init__(self, alt: str, size: int, payload: bytes, source_url: str = "", source_candidates: list[dict[str, Any]] | None = None) -> None:
        self.alt = alt
        self.size = size
        self.payload = payload
        self.source_url = source_url
        self.source_candidates = source_candidates or []
        self.screenshots = 0

    async def is_visible(self) -> bool:
        return True

    async def get_attribute(self, name: str) -> str | None:
        return self.alt if name == "alt" else None

    async def evaluate(self, script: str) -> dict[str, float]:
        del script
        return {
            "complete": True,
            "currentSrc": self.source_url,
            "sourceCandidates": self.source_candidates,
            "naturalWidth": self.size,
            "naturalHeight": self.size,
            "width": self.size,
            "height": self.size,
        }

    async def screenshot(self, **kwargs: Any) -> bytes:
        del kwargs
        self.screenshots += 1
        return self.payload


class _FakeAvatarImages:
    def __init__(self, images: list[_FakeAvatarImage]) -> None:
        self.images = images

    async def count(self) -> int:
        return len(self.images)

    def nth(self, index: int) -> _FakeAvatarImage:
        return self.images[index]


class _FakeAvatarHeader:
    def __init__(self, images: list[_FakeAvatarImage]) -> None:
        self.images = _FakeAvatarImages(images)

    def locator(self, selector: str) -> _FakeAvatarImages:
        if selector != "img":
            raise AssertionError(selector)
        return self.images


class _FakeAvatarResponse:
    ok = True
    headers = {"content-type": "image/png"}

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def body(self) -> bytes:
        return self.payload


class _FakeAvatarRequestContext:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeAvatarResponse:
        del kwargs
        self.urls.append(url)
        return _FakeAvatarResponse(self.payload)


class PersonRecognitionUnitTest(unittest.TestCase):
    def test_exact_profile_embedded_payload_exposes_only_one_stable_user_id(self) -> None:
        evidence = extract_embedded_profile_evidence(
            (
                {
                    "data": {
                        "user": {
                            "username": "target.account",
                            "pk": 123456789012345,
                            "is_private": False,
                        },
                        "suggested_user": {
                            "username": "other.account",
                            "pk": 999999999999999,
                        },
                    }
                },
            ),
            "target.account",
        )
        self.assertEqual("123456789012345", evidence.instagram_user_id)
        self.assertFalse(evidence.is_private)

    def test_categories_are_limited_and_chinese_labels_are_supported(self) -> None:
        self.assertEqual("male", normalize_person_category("男"))
        self.assertEqual("female", normalize_person_category("女"))
        self.assertEqual("couple", normalize_person_category("合拍"))
        self.assertEqual("unknown", normalize_person_category("logo"))
        self.assertEqual("couple", normalize_recognition({"category": "mixed"}).category)

    def test_result_request_normalizes_and_reconciles_both_category_views(self) -> None:
        request = ResultRequest(
            target_id="target",
            username="schema_candidate",
            source_mode="followers",
            profile={"person_category": "女"},
            screening={
                "stage": "basic",
                "person_recognition": {
                    "checked": True,
                    "category": "woman",
                },
            },
        )
        self.assertEqual("female", request.profile["person_category"])
        self.assertEqual(
            "female",
            request.screening["person_recognition"]["category"],
        )
        self.assertEqual("basic", request.screening["stage"])
        self.assertTrue(request.screening["person_recognition"]["checked"])

    def test_result_request_reduces_conflicting_or_invalid_categories_to_unknown(self) -> None:
        for profile_category, screening_category in (
            ("male", "female"),
            ("not-a-person", "male"),
            ("female", "unsupported"),
        ):
            with self.subTest(
                profile_category=profile_category,
                screening_category=screening_category,
            ):
                request = ResultRequest(
                    target_id="target",
                    username="schema_candidate",
                    source_mode="followers",
                    profile={"person_category": profile_category},
                    screening={
                        "person_recognition": {"category": screening_category}
                    },
                )
                self.assertEqual("unknown", request.profile["person_category"])
                self.assertEqual(
                    "unknown",
                    request.screening["person_recognition"]["category"],
                )

        malformed = ResultRequest(
            target_id="target",
            username="schema_candidate",
            source_mode="followers",
            profile={"person_category": "male"},
            screening={"person_recognition": "male"},
        )
        self.assertEqual("unknown", malformed.profile["person_category"])
        self.assertEqual(
            {"category": "unknown"},
            malformed.screening["person_recognition"],
        )

    def test_missing_optional_model_returns_unknown_without_loading_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_face = Path(temp_dir) / "missing-face.xml"
            missing_gender = Path(temp_dir) / "missing-gender.xml"
            classifier = LocalOpenVinoPersonClassifier(missing_face, missing_gender)
            result = classifier.classify(
                b"not-decoded-because-model-is-missing"
            )
        self.assertEqual("unknown", result.category)
        self.assertFalse(result.checked)
        self.assertEqual("model_unavailable", result.reason)

    def test_no_avatar_returns_unknown_before_model_access(self) -> None:
        classifier = LocalOpenVinoPersonClassifier(
            "not-present-face.xml",
            "not-present-gender.xml",
        )
        result = classifier.classify(b"")
        self.assertEqual("unknown", result.category)
        self.assertEqual("avatar_unavailable", result.reason)

    def test_schema_exposes_explicit_local_switch_with_safe_default(self) -> None:
        self.assertFalse(TaskSettingsRequest().local_person_recognition)
        request = DesktopTaskCreateRequest(
            targets=["source"],
            window_ids=["window"],
            modes=["followers"],
            local_person_recognition=True,
        )
        self.assertTrue(request.local_person_recognition)
        self.assertFalse(request.auto_classify)

    def test_avatar_bytes_are_ephemeral_and_removed_from_worker_cache(self) -> None:
        profile = VisibleProfile(
            "avatar_user",
            "public",
            10,
            20,
            3,
            avatar_image_bytes=b"private-rendered-bytes",
        )
        self.assertNotIn("avatar_image_bytes", profile.as_dict())
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker._remember_profile("avatar_user", profile)
        self.assertIsNone(worker._profile_base_cache["avatar_user"].avatar_image_bytes)
        self.assertEqual(b"private-rendered-bytes", profile.avatar_image_bytes)

    def test_bundled_model_files_match_pinned_sha384(self) -> None:
        classifier = LocalOpenVinoPersonClassifier()
        self.assertEqual("available", classifier._model_status())
        self.assertEqual(
            {
                "face-detection-retail-0004.xml",
                "face-detection-retail-0004.bin",
                "age-gender-recognition-retail-0013.xml",
                "age-gender-recognition-retail-0013.bin",
                "person-detection-retail-0013.xml",
                "person-detection-retail-0013.bin",
                "person-attributes-recognition-crossroad-0230.xml",
                "person-attributes-recognition-crossroad-0230.bin",
            },
            set(classifier.BUNDLED_SHA384),
        )
        self.assertEqual(
            set(classifier.BUNDLED_SHA384),
            set(classifier.BUNDLED_SIZE),
        )

    def test_cached_available_model_is_rechecked_before_first_compilation(self) -> None:
        from PIL import Image

        source = LocalOpenVinoPersonClassifier.ASSET_DIRECTORY
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "采集端模型" / "人物识别"
            destination.mkdir(parents=True)
            for filename in LocalOpenVinoPersonClassifier.BUNDLED_SHA384:
                shutil.copy2(source / filename, destination / filename)
            classifier = LocalOpenVinoPersonClassifier(
                destination / "face-detection-retail-0004.xml",
                destination / "age-gender-recognition-retail-0013.xml",
            )
            with patch.object(
                LocalOpenVinoPersonClassifier,
                "ASSET_DIRECTORY",
                destination,
            ):
                self.assertEqual("available", classifier._model_status())
                changed = destination / "age-gender-recognition-retail-0013.bin"
                payload = bytearray(changed.read_bytes())
                payload[0] ^= 0xFF
                changed.write_bytes(payload)

                sample = io.BytesIO()
                Image.new("RGB", (96, 96), (127, 127, 127)).save(
                    sample,
                    format="PNG",
                )
                result = classifier.classify(sample.getvalue())

        self.assertIsNone(classifier._compiled_models)
        self.assertEqual("unknown", result.category)
        self.assertFalse(result.checked)
        self.assertEqual("model_integrity_failed", result.reason)

    def test_ir_loader_never_decodes_binary_weights_or_passes_unicode_path(self) -> None:
        class RecordingCore:
            def __init__(self) -> None:
                self.calls: list[tuple[bytes, bytes]] = []

            def read_model(self, *, model: bytes, weights: bytes) -> object:
                self.calls.append((model, weights))
                return object()

        with tempfile.TemporaryDirectory() as temporary:
            model_directory = Path(temporary) / "新建文件夹（4）" / "人物模型"
            model_directory.mkdir(parents=True)
            xml_path = model_directory / "sample.xml"
            xml_bytes = b'<?xml version="1.0"?><net />'
            # 0xD4 is invalid as the first byte of a UTF-8 string and mirrors
            # the Windows error that this in-memory loader must avoid.
            weights_bytes = b"\xd4\xd8\x00\xffbinary-weights"
            xml_path.write_bytes(xml_bytes)
            xml_path.with_suffix(".bin").write_bytes(weights_bytes)
            core = RecordingCore()
            loaded = read_openvino_ir_model_from_memory(core, xml_path)

        self.assertIsNotNone(loaded)
        self.assertEqual([(xml_bytes, weights_bytes)], core.calls)

    @unittest.skipUnless(
        sys.platform == "win32",
        "production OpenVINO native bootstrap is intentionally Windows-only",
    )
    def test_real_classifier_runs_from_unicode_model_directory(self) -> None:
        from PIL import Image

        source = LocalOpenVinoPersonClassifier.ASSET_DIRECTORY
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "聚鑫采集器" / "新建文件夹（4）" / "模型"
            destination.mkdir(parents=True)
            for filename in LocalOpenVinoPersonClassifier.BUNDLED_SHA384:
                shutil.copy2(source / filename, destination / filename)
            classifier = LocalOpenVinoPersonClassifier(
                destination / "face-detection-retail-0004.xml",
                destination / "age-gender-recognition-retail-0013.xml",
            )
            _, compiled_gender, _, _ = classifier._get_compiled_models()
            parsed_category, parsed_confidence = (
                exercise_local_openvino_gender_branch(
                    compiled_gender,
                    face_model_path=classifier.face_model_path,
                    gender_model_path=classifier.gender_model_path,
                )
            )
            sample = io.BytesIO()
            Image.new("RGB", (96, 96), (127, 127, 127)).save(sample, format="PNG")
            result = classifier.classify(sample.getvalue())

        self.assertIn(parsed_category, {"male", "female"})
        self.assertGreaterEqual(parsed_confidence, 0.0)
        self.assertTrue(result.checked)
        self.assertEqual("unknown", result.category)
        # The neutral synthetic image sits on the detector's numerical
        # boundary.  Different valid CPU dispatch paths may reject it at the
        # person stage or at the face-quality stage; both are safe, checked
        # outcomes and neither is a model/runtime failure.
        self.assertIn(result.reason, {"no_person_detected", "no_reliable_face"})

    def test_male_and_female_faces_combine_as_couple(self) -> None:
        classifier = _ModelLogicClassifier(
            [("female", 0.91, 27), ("male", 0.93, 31)]
        )
        result = classifier.classify(b"rendered-avatar")
        self.assertEqual("couple", result.category)
        self.assertTrue(result.checked)
        self.assertEqual(2, result.face_count)

    def test_female_probability_over_half_and_female_group_rule(self) -> None:
        single = _ModelLogicClassifier(
            [("female", 0.51, 27)]
        ).classify(b"single-avatar")
        female_group = _ModelLogicClassifier(
            [("female", 0.51, 27), ("female", 0.83, 31)]
        ).classify(b"group-avatar")
        male_group = _ModelLogicClassifier(
            [("male", 0.51, 27), ("male", 0.74, 31)]
        ).classify(b"male-group-avatar")

        self.assertEqual("female", single.category)
        self.assertEqual("couple", female_group.category)
        self.assertEqual("unknown", male_group.category)
        self.assertFalse(male_group.male_exclusion_confirmed)

    def test_ambiguous_face_is_not_forced_to_male(self) -> None:
        import numpy
        from PIL import Image

        classifier = LocalOpenVinoPersonClassifier(
            male_face_probability_threshold=0.88
        )
        classifier._infer_outputs = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "prob": numpy.asarray([0.35, 0.65], dtype=numpy.float32),
            "age_conv3": numpy.asarray([0.30], dtype=numpy.float32),
        }
        parsed = classifier._classify_faces(
            object(),
            Image.new("RGB", (96, 96), "white"),
            [(8, 8, 88, 88, 0.95)],
        )
        self.assertEqual([], parsed)
        self.assertEqual(
            1,
            classifier._diagnostics.classification[
                "low_gender_confidence_count"
            ],
        )

    def test_default_avatar_gender_gate_classifies_at_least_55_percent_male(self) -> None:
        import numpy
        from PIL import Image

        classifier = LocalOpenVinoPersonClassifier()
        probabilities = iter(([0.44, 0.56], [0.44, 0.56], [0.46, 0.54], [0.46, 0.54]))

        def infer(*_args: object, **_kwargs: object) -> dict[str, Any]:
            pair = next(probabilities)
            return {
                "prob": numpy.asarray(pair, dtype=numpy.float32),
                "age_conv3": numpy.asarray([0.30], dtype=numpy.float32),
            }

        classifier._infer_outputs = infer  # type: ignore[method-assign]
        image = Image.new("RGB", (96, 96), "white")
        above = classifier._classify_faces(
            object(), image, [(8, 8, 88, 88, 0.95)]
        )
        ambiguous = classifier._classify_faces(
            object(), image, [(8, 8, 88, 88, 0.95)]
        )

        self.assertEqual("male", above[0][0])
        self.assertGreater(above[0][1], 0.55)
        self.assertEqual([], ambiguous)

    def test_female_over_half_remains_female_with_asymmetric_gate(self) -> None:
        import numpy
        from PIL import Image

        classifier = LocalOpenVinoPersonClassifier(
            male_face_probability_threshold=0.88
        )
        classifier._infer_outputs = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "prob": numpy.asarray([0.51, 0.49], dtype=numpy.float32),
            "age_conv3": numpy.asarray([0.30], dtype=numpy.float32),
        }
        parsed = classifier._classify_faces(
            object(),
            Image.new("RGB", (96, 96), "white"),
            [(8, 8, 88, 88, 0.95)],
        )
        self.assertEqual("female", parsed[0][0])

    def test_conflicting_mirrored_side_pose_remains_unknown(self) -> None:
        import numpy
        from PIL import Image

        classifier = LocalOpenVinoPersonClassifier(
            male_face_probability_threshold=0.88
        )
        probabilities = iter(([0.45, 0.55], [0.65, 0.35]))

        def infer(*_args: object, **_kwargs: object) -> dict[str, Any]:
            pair = next(probabilities)
            return {
                "prob": numpy.asarray(pair, dtype=numpy.float32),
                "age_conv3": numpy.asarray([0.30], dtype=numpy.float32),
            }

        classifier._infer_outputs = infer  # type: ignore[method-assign]
        parsed = classifier._classify_faces(
            object(),
            Image.new("RGB", (96, 96), "white"),
            [(8, 8, 88, 88, 0.95)],
        )
        self.assertEqual([], parsed)

    def test_body_fallback_handles_background_and_group_rules(self) -> None:
        scenery = _BodyFallbackLogicClassifier([]).classify(b"scenery")
        woman = _BodyFallbackLogicClassifier([("female", 0.51)]).classify(b"body")
        group = _BodyFallbackLogicClassifier(
            [("male", 0.88), ("female", 0.52)]
        ).classify(b"group")

        self.assertEqual("unknown", scenery.category)
        self.assertEqual("no_person_detected", scenery.reason)
        self.assertEqual("female", woman.category)
        self.assertEqual("body_attribute_fallback", woman.reason)
        self.assertEqual("local_openvino_body_fallback", woman.source)
        self.assertEqual("couple", group.category)
        self.assertEqual(2, group.body_count)

    def test_child_only_and_adult_plus_child_are_conservative(self) -> None:
        child_only = _ModelLogicClassifier(
            [("male", 0.96, 12)]
        ).classify(b"avatar")
        adult_plus_child = _ModelLogicClassifier(
            [("female", 0.92, 29), ("male", 0.97, 10)]
        ).classify(b"avatar")
        unknown = _ModelLogicClassifier([]).classify(b"avatar")
        self.assertEqual("unknown", child_only.category)
        self.assertEqual("female", adult_plus_child.category)
        self.assertEqual("unknown", unknown.category)
        self.assertTrue(unknown.checked)
        self.assertEqual("no_person_detected", unknown.reason)

    def test_multi_output_model_requires_exact_prob_and_age_names(self) -> None:
        class Port:
            any_name = "unexpected_output"

            def get_names(self) -> set[str]:
                return {"unexpected_output"}

        port = Port()

        class Request:
            def infer(self, inputs: dict[object, object]) -> dict[object, object]:
                del inputs
                return {port: [0.5]}

        class Compiled:
            outputs = [port]

            def input(self, index: int) -> str:
                self.assert_zero(index)
                return "input"

            @staticmethod
            def assert_zero(index: int) -> None:
                if index != 0:
                    raise AssertionError(index)

            def create_infer_request(self) -> Request:
                return Request()

        with self.assertRaises(ValueError):
            LocalOpenVinoPersonClassifier._infer_outputs(
                Compiled(),
                object(),
                ("prob", "age_conv3"),
            )

    def test_face_floor_uses_detector_scale_not_small_avatar_source_scale(self) -> None:
        from PIL import Image

        image = Image.new("RGB", (100, 100), "white")
        rejected = _DetectionOutputClassifier(0.039)._detect_faces(object(), image)
        accepted = _DetectionOutputClassifier(0.10)._detect_faces(object(), image)
        self.assertEqual([], rejected)
        self.assertEqual(1, len(accepted))

        small_avatar = Image.new("RGB", (64, 64), "white")
        clear_portrait = _DetectionOutputClassifier(0.32)._detect_faces(
            object(), small_avatar
        )
        self.assertEqual(1, len(clear_portrait))


class PersonRecognitionExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_target_uses_high_resolution_current_src_before_screenshot(self) -> None:
        from PIL import Image

        high_resolution = io.BytesIO()
        Image.new("RGB", (320, 320), "white").save(high_resolution, format="PNG")
        source_url = "https://scontent.cdninstagram.com/target-avatar.png"
        target = _FakeAvatarImage(
            "candidate's profile picture",
            64,
            b"low-resolution-screenshot",
            source_url,
        )
        request = _FakeAvatarRequestContext(high_resolution.getvalue())
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = SimpleNamespace(context=SimpleNamespace(request=request))

        captured = await worker._capture_visible_profile_avatar_detailed(
            _FakeAvatarHeader([target]),
            "candidate",
        )

        self.assertEqual(high_resolution.getvalue(), captured.payload)
        self.assertEqual("current_src", captured.source)
        self.assertEqual((320, 320), (captured.image_width, captured.image_height))
        self.assertEqual([source_url], request.urls)
        self.assertEqual(0, target.screenshots)

    async def test_exact_target_prefers_largest_srcset_candidate(self) -> None:
        from PIL import Image

        high_resolution = io.BytesIO()
        Image.new("RGB", (640, 640), "white").save(high_resolution, format="PNG")
        current_url = "https://scontent.cdninstagram.com/avatar-150.png"
        largest_url = "https://scontent.cdninstagram.com/avatar-640.png"
        target = _FakeAvatarImage(
            "candidate's profile picture",
            64,
            b"low-resolution-screenshot",
            current_url,
            [
                {"url": current_url, "score": 150},
                {"url": largest_url, "score": 640},
            ],
        )
        request = _FakeAvatarRequestContext(high_resolution.getvalue())
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = SimpleNamespace(context=SimpleNamespace(request=request))

        captured = await worker._capture_visible_profile_avatar_detailed(
            _FakeAvatarHeader([target]),
            "candidate",
        )

        self.assertEqual([largest_url], request.urls)
        self.assertEqual((640, 640), (captured.image_width, captured.image_height))
        self.assertEqual(0, target.screenshots)

    async def test_exact_target_avatar_beats_larger_recommendation(self) -> None:
        target = _FakeAvatarImage(
            "candidate's profile picture",
            64,
            b"target-avatar",
        )
        recommendation = _FakeAvatarImage(
            "recommended_user's profile picture",
            240,
            b"wrong-avatar",
        )
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        captured = await worker._capture_visible_profile_avatar(
            _FakeAvatarHeader([recommendation, target]),
            "candidate",
        )
        self.assertEqual(b"target-avatar", captured)
        self.assertEqual(1, target.screenshots)
        self.assertEqual(0, recommendation.screenshots)

    async def test_ambiguous_unlabelled_header_images_return_unknown(self) -> None:
        first = _FakeAvatarImage("", 80, b"first")
        second = _FakeAvatarImage("", 120, b"second")
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        captured = await worker._capture_visible_profile_avatar(
            _FakeAvatarHeader([first, second]),
            "candidate",
        )
        self.assertIsNone(captured)
        self.assertEqual(0, first.screenshots + second.screenshots)

    async def test_recommendation_word_inside_exact_username_is_not_rejected(self) -> None:
        target = _FakeAvatarImage(
            "recommended_user's profile picture",
            96,
            b"target-avatar",
        )
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        captured = await worker._capture_visible_profile_avatar(
            _FakeAvatarHeader([target]),
            "recommended_user",
        )
        self.assertEqual(b"target-avatar", captured)
        self.assertEqual(1, target.screenshots)

    async def test_single_explicit_recommendation_is_never_avatar_fallback(self) -> None:
        recommendation = _FakeAvatarImage(
            "recommended_user's suggested profile picture",
            240,
            b"wrong-avatar",
        )
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        captured = await worker._capture_visible_profile_avatar(
            _FakeAvatarHeader([recommendation]),
            "candidate",
        )
        self.assertIsNone(captured)
        self.assertEqual(0, recommendation.screenshots)

    async def test_legacy_enabled_recognition_is_ignored_before_record_result(self) -> None:
        service, classifier, worker = _RecordingService(), _StaticClassifier("female"), _ProfileWorker()
        manager = ExecutionManager(service, object(), person_classifier_factory=lambda: classifier)
        self.assertTrue(await manager._screen_and_record(_execution_control(), worker,
            "target", "candidate", "followers", {"local_person_recognition": True, "exclude_male_avatar": True}))
        self.assertEqual(1, len(service.results))
        self.assertEqual("unknown", service.results[0]["profile"]["person_category"])
        self.assertNotIn("person_recognition", service.results[0]["screening"])
        self.assertEqual([], classifier.images)
        self.assertFalse(any(worker.avatar_capture_requests))

    async def test_retired_classifier_cannot_fail_or_block_result(self) -> None:
        service = _RecordingService()
        def forbidden(): raise AssertionError("classifier construction is retired")
        manager = ExecutionManager(service, object(), person_classifier_factory=forbidden)
        self.assertTrue(await manager._screen_and_record(_execution_control(), _ProfileWorker(),
            "target", "candidate", "followers", {"local_person_recognition": True}))
        self.assertEqual("unknown", service.results[0]["profile"]["person_category"])
        self.assertNotIn("person_recognition", service.results[0]["screening"])

    async def test_non_us_location_runs_after_counts_and_skips_classifier(self) -> None:
        service = _RecordingService()
        classifier = _StaticClassifier("male")
        worker = _DeferredNonUsProfileWorker()
        manager = ExecutionManager(
            service,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=lambda: classifier,
        )
        await manager._screen_and_record(
            _execution_control(),  # type: ignore[arg-type]
            worker,
            "target",
            "candidate",
            "followers",
            {
                "local_person_recognition": True,
                "location_enabled": True,
                "mode_limits": {"followers": {}},
            },
        )

        self.assertEqual("location_rejected", service.results[0]["screening"]["stage"])
        self.assertEqual("加拿大", service.results[0]["profile"]["location_zh"])
        self.assertEqual(100, service.results[0]["profile"]["followers"])
        self.assertEqual(["candidate"], worker.profile_reads)
        self.assertEqual([False], worker.avatar_capture_requests)
        self.assertEqual(0, worker.deferred_avatar_requests)
        self.assertEqual([], classifier.images)
        self.assertNotIn("person_recognition", service.results[0]["screening"])

    async def test_stable_id_duplicate_stops_pipeline_before_result_or_review(self) -> None:
        service = _StableIdentityDuplicateService()
        manager = ExecutionManager(service, object())  # type: ignore[arg-type]
        saved = await manager._screen_and_record(
            _execution_control(),  # type: ignore[arg-type]
            _StableIdProfileWorker(),
            "target",
            "renamed.account",
            "followers",
            {
                "location_enabled": False,
                "mode_limits": {"followers": {}},
            },
            claim_id="claimed-account-id",
        )

        self.assertFalse(saved)
        self.assertEqual([], service.results)
        self.assertEqual(1, len(service.confirmations))
        self.assertEqual(
            "123456789012345",
            service.confirmations[0]["instagram_user_id"],
        )

    async def test_old_profile_category_cannot_restore_gender_judgment(self) -> None:
        class OldProfile(_ProfileWorker):
            async def read_visible_profile(self, username, **kwargs):
                return {**await super().read_visible_profile(username, **kwargs), "person_category": "female"}
        service = _RecordingService()
        manager = ExecutionManager(service, object(), person_classifier_factory=_InvalidConfidenceClassifier)
        await manager._screen_and_record(_execution_control(), OldProfile(),
            "target", "candidate", "followers", {"local_person_recognition": True})
        self.assertEqual("unknown", service.results[0]["profile"]["person_category"])
        self.assertNotIn("person_recognition", service.results[0]["screening"])

    async def test_disabled_switch_never_constructs_classifier(self) -> None:
        constructed = False

        def factory() -> _StaticClassifier:
            nonlocal constructed
            constructed = True
            return _StaticClassifier("male")

        manager = ExecutionManager(
            _RecordingService(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=factory,
        )
        profile: dict[str, Any] = {"avatar_url": "https://example.invalid/a.jpg"}
        screening: dict[str, Any] = {}
        await manager._apply_person_recognition(profile, screening, {})

        self.assertFalse(constructed)
        self.assertEqual("unknown", profile["person_category"])
        self.assertFalse(screening["person_recognition"]["enabled"])
        self.assertEqual("disabled", screening["person_recognition"]["reason"])

    async def test_privacy_auto_classify_does_not_enable_person_recognition(self) -> None:
        classifier = _StaticClassifier("couple")
        manager = ExecutionManager(
            _RecordingService(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=lambda: classifier,
        )
        profile: dict[str, Any] = {"avatar_url": "https://example.invalid/a.jpg"}
        screening: dict[str, Any] = {}
        await manager._apply_person_recognition(
            profile,
            screening,
            {"auto_classify": True},
        )

        self.assertEqual("unknown", profile["person_category"])
        self.assertEqual("unknown", screening["person_recognition"]["category"])
        self.assertFalse(screening["person_recognition"]["enabled"])
        self.assertEqual([], classifier.images)

    async def test_disabled_setting_does_not_request_browser_avatar_capture(self) -> None:
        worker = _ProfileWorker()
        service = _RecordingService()
        manager = ExecutionManager(
            service,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=lambda: _StaticClassifier("male"),
        )
        await manager._screen_and_record(
            _execution_control(),  # type: ignore[arg-type]
            worker,
            "target",
            "candidate",
            "followers",
            {"mode_limits": {"followers": {}}},
        )
        self.assertEqual([False, False], worker.avatar_capture_requests)
        self.assertEqual("unknown", service.results[0]["profile"]["person_category"])

    async def test_executor_submission_failure_returns_slot_and_becomes_unknown(self) -> None:
        class RejectingExecutor:
            @staticmethod
            def submit(*args: Any, **kwargs: Any) -> None:
                del args, kwargs
                raise RuntimeError("executor is shutting down")

        manager = ExecutionManager(
            _RecordingService(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=lambda: _StaticClassifier("male"),
        )
        profile: dict[str, Any] = {}
        screening: dict[str, Any] = {}
        with patch(
            "app.execution_manager._PERSON_INFERENCE_EXECUTOR",
            RejectingExecutor(),
        ):
            await manager._apply_person_recognition(
                profile,
                screening,
                {"local_person_recognition": True},
                avatar_image_bytes=b"avatar",
            )
        self.assertEqual("unknown", profile["person_category"])
        self.assertEqual(
            "classifier_failed",
            screening["person_recognition"]["reason"],
        )
        self.assertEqual(2, manager._person_inference_slots._value)

    async def test_retired_blocking_classifier_never_starts_or_delays_candidate(self) -> None:
        service, classifier = _RecordingService(), _BlockingClassifier("female")
        manager = ExecutionManager(service, object(), person_classifier_factory=lambda: classifier,
                                   person_recognition_timeout_seconds=0.02)
        try:
            await asyncio.wait_for(manager._screen_and_record(_execution_control(), _ProfileWorker(),
                "target", "candidate", "followers", {"local_person_recognition": True}), 2)
            self.assertFalse(classifier.started.is_set())
            self.assertEqual(1, len(service.results))
        finally: classifier.release.set()

    async def test_retired_classifier_cannot_cross_into_following_accounts(self) -> None:
        service, classifier, worker = _RecordingService(), _BlockingClassifier("female"), _ProfileWorker()
        manager = ExecutionManager(service, object(), person_classifier_factory=lambda: classifier)
        try:
            for username in ('first', 'second'):
                await manager._screen_and_record(_execution_control(), worker,
                    "target", username, "followers", {"local_person_recognition": True})
            self.assertFalse(classifier.started.is_set())
            self.assertEqual(['first', 'second'], [row['username'] for row in service.results])
            self.assertEqual(['unknown', 'unknown'], [row['profile']['person_category'] for row in service.results])
        finally: classifier.release.set()

    async def test_stop_during_activity_prevents_late_result_write_without_gender_inference(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        class Worker(_ProfileWorker):
            async def read_visible_profile(self, username, **kwargs):
                if kwargs.get('include_activity'):
                    entered.set(); await release.wait()
                return await super().read_visible_profile(username, **kwargs)
        service = _RecordingService()
        manager = ExecutionManager(service, object())
        pending = asyncio.create_task(manager._screen_and_record(_execution_control(), Worker(),
            "target", "candidate", "followers", {"local_person_recognition": True}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError): await pending
            self.assertEqual([], service.results)
        finally:
            release.set(); pending.cancel(); await asyncio.gather(pending, return_exceptions=True)

    async def test_many_windows_wait_for_slots_without_false_timeouts(self) -> None:
        classifier = _ConcurrencyClassifier(delay=0.025)
        manager = ExecutionManager(
            _RecordingService(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=lambda: classifier,
            person_recognition_timeout_seconds=0.2,
        )
        profiles = [{} for _ in range(12)]
        screenings = [{} for _ in profiles]
        await asyncio.gather(
            *(
                manager._apply_person_recognition(
                    profile,
                    screening,
                    {"local_person_recognition": True},
                    avatar_image_bytes=b"avatar",
                )
                for profile, screening in zip(profiles, screenings, strict=True)
            )
        )
        self.assertLessEqual(classifier.maximum_active, 2)
        self.assertTrue(all(profile["person_category"] == "female" for profile in profiles))
        self.assertTrue(
            all("reason" not in screening["person_recognition"] for screening in screenings)
        )

    async def test_cancelled_slot_waiter_cannot_steal_a_permit(self) -> None:
        classifier = _TwoSlotBlockingClassifier()
        manager = ExecutionManager(
            _RecordingService(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=lambda: classifier,
            person_recognition_timeout_seconds=2,
        )

        async def recognize() -> dict[str, Any]:
            profile: dict[str, Any] = {}
            await manager._apply_person_recognition(
                profile,
                {},
                {"local_person_recognition": True},
                avatar_image_bytes=b"avatar",
            )
            return profile

        first = asyncio.create_task(recognize())
        second = asyncio.create_task(recognize())
        for _ in range(100):
            if classifier.two_started.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(classifier.two_started.is_set())
        waiting = asyncio.create_task(recognize())
        await asyncio.sleep(0.02)
        self.assertFalse(waiting.done())
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

        classifier.release.set()
        await asyncio.gather(first, second)
        followups = await asyncio.wait_for(
            asyncio.gather(recognize(), recognize()),
            timeout=0.5,
        )
        self.assertEqual(["female", "female"], [item["person_category"] for item in followups])

    async def test_retired_recognition_does_not_run_with_optional_gpt(self) -> None:
        events: list[str] = []
        manager = ExecutionManager(
            _RecordingService(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            person_classifier_factory=lambda: _EventClassifier(events),
            reviewer_factory=lambda: _EventReviewer(events),
        )
        await manager._screen_and_record(
            _execution_control(),  # type: ignore[arg-type]
            _ProfileWorker(),
            "target",
            "candidate",
            "followers",
            {
                "local_person_recognition": True,
                "gpt_enabled": True,
                "mode_limits": {"followers": {}},
            },
        )
        self.assertEqual(["gpt"], events)


class PersonRecognitionPausePipelineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "pause.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.user = self.service.register_user(
            "recognition-pause",
            "person recognition pause password",
        )
        self.task = self.service.create_task(
            self.user["id"],
            name="pause recognition",
            modes=["followers"],
            targets=["pause_source"],
            window_ids=["pause-window"],
            settings={
                "local_person_recognition": True,
                "mode_limits": {"followers": {"per_target_limit": 10}},
            },
        )
        self.target_id = self.task["targets"][0]["id"]
        self.service.append_task_mode_candidates(
            self.user["id"],
            self.task["id"],
            self.target_id,
            "followers",
            ["pause_first", "pause_second"],
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_pause_after_activity_holds_write_and_next_profile_read(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        class Worker(_ProfileWorker):
            async def read_visible_profile(self, username, **kwargs):
                value = await super().read_visible_profile(username, **kwargs)
                if username == 'pause_first' and kwargs.get('include_activity'):
                    entered.set(); await release.wait()
                return value
        worker = Worker()
        manager = ExecutionManager(self.service, object())
        pause = asyncio.Event(); pause.set()
        control = SimpleNamespace(owner_user_id=self.user['id'], task_id=self.task['id'],
            pause_event=pause, stop_event=asyncio.Event(), last_progress_at=None)
        draining = asyncio.create_task(manager._drain_candidate_spool(control, worker, self.target_id,
            'followers', self.task['settings'], discovery_complete=True))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pause.clear(); release.set()
            await asyncio.sleep(.05)
            self.assertEqual([], self.service.list_results(self.user['id'], self.task['id']))
            self.assertEqual(['pause_first', 'pause_first'], worker.profile_reads)
            pause.set()
            stats = await asyncio.wait_for(draining, 5)
            self.assertEqual(2, stats['recorded'])
            self.assertEqual(['pause_first', 'pause_first', 'pause_second', 'pause_second'], worker.profile_reads)
        finally:
            pause.set(); release.set(); draining.cancel(); await asyncio.gather(draining, return_exceptions=True)


class PersonRecognitionBackfillTest(unittest.TestCase):
    PASSWORD = "person recognition backfill password"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "recognition.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.user = self.service.register_user("recognition-owner", self.PASSWORD)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _task(self, source: str, *, recognition: bool) -> dict[str, Any]:
        return self.service.create_task(
            self.user["id"],
            name=f"recognition-{source}",
            modes=["followers"],
            targets=[source],
            window_ids=[f"window-{source}"],
            settings={
                "local_person_recognition": recognition,
                "mode_limits": {"followers": {"per_target_limit": 10}},
            },
        )

    def _record(
        self,
        task: dict[str, Any],
        username: str,
        *,
        category: str = "unknown",
        checked: bool = False,
    ) -> dict[str, Any]:
        return self.service.record_result(
            self.user["id"],
            task["id"],
            task["targets"][0]["id"],
            username=username,
            instagram_user_id=None,
            source_mode="followers",
            visibility="public",
            profile={"username": username, "person_category": category},
            screening={
                "person_recognition": {
                    "enabled": True,
                    "checked": checked,
                    "category": category,
                }
            },
            qualified=True,
        )

    def test_setting_is_validated_and_persisted(self) -> None:
        validated = validate_task_settings({"local_person_recognition": True})
        self.assertFalse(validated["local_person_recognition"])
        task = self._task("setting_source", recognition=True)
        self.assertFalse(task["settings"]["local_person_recognition"])

    def test_record_result_normalizes_one_category_view_into_both_without_mutating_input(self) -> None:
        task = self._task("normalized_payload", recognition=True)
        profile = {
            "username": "normalized_candidate",
            "person_category": "男",
        }
        screening = {
            "stage": "complete",
            "person_recognition": {
                "checked": True,
                "source": "external_worker",
            },
        }
        saved = self.service.record_result(
            self.user["id"],
            task["id"],
            task["targets"][0]["id"],
            username="normalized_candidate",
            instagram_user_id=None,
            source_mode="followers",
            visibility="public",
            profile=profile,
            screening=screening,
            qualified=True,
        )

        self.assertEqual("male", saved["profile"]["person_category"])
        recognition = saved["screening"]["person_recognition"]
        self.assertEqual("male", recognition["category"])
        self.assertTrue(recognition["checked"])
        self.assertEqual("external_worker", recognition["source"])
        self.assertNotIn("category", screening["person_recognition"])
        self.assertEqual("男", profile["person_category"])

    def test_conflicting_persisted_categories_become_unknown_then_valid_backfill_wins(self) -> None:
        original = self._task("conflict_original", recognition=True)
        first = self.service.record_result(
            self.user["id"],
            original["id"],
            original["targets"][0]["id"],
            username="conflicting_candidate",
            instagram_user_id=None,
            source_mode="followers",
            visibility="public",
            profile={"person_category": "male"},
            screening={
                "person_recognition": {
                    "checked": True,
                    "category": "female",
                }
            },
            qualified=True,
        )
        self.assertEqual("unknown", first["profile"]["person_category"])
        self.assertEqual(
            "unknown",
            first["screening"]["person_recognition"]["category"],
        )
        self.assertFalse(
            self.service.check_global_dedupe(
                "conflicting_candidate",
                owner_user_id=self.user["id"],
            )["person_recognition_needed"]
        )

        retry = self._task("conflict_retry", recognition=True)
        improved = self._record(
            retry,
            "conflicting_candidate",
            category="couple",
            checked=True,
        )
        self.assertTrue(improved["deduped"])
        self.assertTrue(improved["person_recognition_backfilled"])
        canonical = self.service.list_results(
            self.user["id"],
            original["id"],
        )[0]
        self.assertEqual("couple", canonical["profile"]["person_category"])
        self.assertEqual(
            "couple",
            canonical["screening"]["person_recognition"]["category"],
        )

    def test_desktop_api_persists_explicit_local_setting(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI test dependency is not installed in this source-only environment")

        from app.config import Settings
        from app.main import create_app

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            settings = Settings(
                startup_token="recognition-api-startup-token-long-enough",
                database_path=path / "api.sqlite3",
                data_dir=path,
            )
            headers = {"X-Startup-Token": settings.startup_token}
            with TestClient(create_app(settings)) as client:
                client.post(
                    "/v1/auth/register",
                    headers=headers,
                    json={"username": "recognition-api", "password": self.PASSWORD},
                )
                login = client.post(
                    "/v1/auth/login",
                    headers=headers,
                    json={"username": "recognition-api", "password": self.PASSWORD},
                )
                authenticated = headers | {
                    "Authorization": f"Bearer {login.json()['token']}"
                }
                created = client.post(
                    "/api/tasks",
                    headers=authenticated,
                    json={
                        "targets": ["api_source"],
                        "window_ids": ["api-window"],
                        "modes": ["followers"],
                        "source_limits": {
                            "followers": {"perTargetLimit": 1}
                        },
                        "local_person_recognition": True,
                    },
                )
                self.assertEqual(201, created.status_code, created.text)
                task = client.get(
                    f"/api/tasks/{created.json()['task_id']}",
                    headers=authenticated,
                )
                self.assertEqual(200, task.status_code, task.text)
                self.assertFalse(task.json()["settings"]["local_person_recognition"])

    def test_fresh_global_duplicate_skips_network_while_offline_backfill_remains_available(self) -> None:
        original = self._task("first_source", recognition=False)
        self._record(original, "shared_candidate", checked=False)
        before = self.service.list_results(self.user["id"], original["id"])[0]
        original_updated_at = before["updated_at"]

        retry = self._task("retry_source", recognition=True)
        stats = self.service.append_task_mode_candidates(
            self.user["id"],
            retry["id"],
            retry["targets"][0]["id"],
            "followers",
            ["shared_candidate"],
        )
        self.assertEqual(0, stats["pending"])
        dedupe = self.service.check_global_dedupe(
            "shared_candidate",
            owner_user_id=self.user["id"],
        )
        self.assertTrue(dedupe["seen"])
        self.assertFalse(dedupe["person_recognition_needed"])

        saved = self._record(
            retry,
            "shared_candidate",
            category="female",
            checked=True,
        )
        self.assertTrue(saved["deduped"])
        self.assertTrue(saved["person_recognition_backfilled"])
        self.assertEqual([], self.service.list_results(self.user["id"], retry["id"]))
        after = self.service.list_results(self.user["id"], original["id"])[0]
        self.assertEqual("female", after["profile"]["person_category"])
        self.assertEqual(original_updated_at, after["updated_at"])

        reconciled = self.service.reconcile_task_mode_candidates(
            self.user["id"],
            retry["id"],
            retry["targets"][0]["id"],
            "followers",
        )
        self.assertEqual(0, reconciled["pending"])
        self.assertEqual(1, reconciled["deduped"])

    def test_current_semantic_unknown_is_stable_and_not_reopened(self) -> None:
        original = self._task("unknown_source", recognition=True)
        self.service.record_result(
            self.user["id"],
            original["id"],
            original["targets"][0]["id"],
            username="stable_unknown",
            instagram_user_id=None,
            source_mode="followers",
            visibility="public",
            profile={"username": "stable_unknown", "person_category": "unknown"},
            screening={
                "person_recognition": {
                    "enabled": True,
                    "checked": True,
                    "category": "unknown",
                    "reason": "no_face_detected",
                    "recognizer_version": PERSON_RECOGNIZER_VERSION,
                }
            },
            qualified=True,
        )
        retry = self._task("unknown_retry", recognition=True)
        stats = self.service.append_task_mode_candidates(
            self.user["id"],
            retry["id"],
            retry["targets"][0]["id"],
            "followers",
            ["stable_unknown"],
        )
        self.assertEqual(0, stats["pending"])
        self.assertEqual(1, stats["deduped"])
        self.assertFalse(
            self.service.check_global_dedupe(
                "stable_unknown",
                owner_user_id=self.user["id"],
            )["person_recognition_needed"]
        )

    def test_legacy_checked_unknown_never_requests_profile_reopen(self) -> None:
        original = self._task("legacy_unknown_source", recognition=True)
        self._record(original, "legacy_unknown", checked=True)
        self.assertFalse(
            self.service.check_global_dedupe(
                "legacy_unknown",
                owner_user_id=self.user["id"],
            )["person_recognition_needed"]
        )

    def test_restart_crash_gap_records_rank_zero_result_without_reopening(self) -> None:
        task = self._task("crash_source", recognition=True)
        target_id = task["targets"][0]["id"]
        self.service.append_task_mode_candidates(
            self.user["id"], task["id"], target_id, "followers", ["crash_candidate"]
        )
        self._record(task, "crash_candidate", checked=False)
        # Simulate a process exit after record_result and before finish candidate.
        stats = self.service.reconcile_task_mode_candidates(
            self.user["id"], task["id"], target_id, "followers"
        )
        self.assertEqual(0, stats["pending"])

        self._record(task, "crash_candidate", category="male", checked=True)
        stats = self.service.reconcile_task_mode_candidates(
            self.user["id"], task["id"], target_id, "followers"
        )
        self.assertEqual(0, stats["pending"])
        self.assertEqual(1, stats["recorded"])

    def test_late_success_atomically_beats_early_and_late_failures(self) -> None:
        original = self._task("race_original", recognition=False)
        self._record(original, "race_candidate", checked=False)
        retry = self._task("race_retry", recognition=True)
        failure_done = threading.Event()

        def early_failure() -> dict[str, Any]:
            try:
                return self._record(retry, "race_candidate", checked=False)
            finally:
                failure_done.set()

        def late_success() -> dict[str, Any]:
            failure_done.wait(timeout=2)
            return self._record(
                retry,
                "race_candidate",
                category="couple",
                checked=True,
            )

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            failure_result = executor.submit(early_failure).result(timeout=3)
            success_result = executor.submit(late_success).result(timeout=3)
        self.assertFalse(failure_result["person_recognition_backfilled"])
        self.assertTrue(success_result["person_recognition_backfilled"])

        # A failure arriving after success may update dedupe metadata, never the
        # canonical recognition payload.
        self._record(retry, "race_candidate", checked=False)
        canonical = self.service.list_results(self.user["id"], original["id"])[0]
        self.assertEqual("couple", canonical["profile"]["person_category"])
        self.assertTrue(canonical["screening"]["person_recognition"]["checked"])


if __name__ == "__main__":
    unittest.main()
