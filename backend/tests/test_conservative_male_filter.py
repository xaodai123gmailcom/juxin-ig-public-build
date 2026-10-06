from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.execution_manager import ExecutionManager
from app.person_recognition import LocalOpenVinoPersonClassifier, normalize_recognition


class AvatarClassifier(LocalOpenVinoPersonClassifier):
    """Exercise production classification using explicit model outputs and pixels."""

    def __init__(self, views, *, faces=None, image=None, bodies=None):
        super().__init__()
        self.views = iter(views)
        self.faces = faces if faces is not None else [(8, 8, 88, 88, .95)]
        self.image = image if image is not None else Image.fromarray(
            np.random.default_rng(9).integers(0, 256, (96, 96, 3), dtype=np.uint8)
        )
        self.bodies = bodies or []
        self.calls = 0

    def _model_status(self):
        return "available"

    def _decode_image(self, _payload):
        return self.image

    def _get_compiled_models(self):
        return (None,) * 4

    def _detect_faces(self, *_args):
        return self.faces

    def _detect_people(self, *_args):
        return [(0, 0, 80, 96, .99)] if self.bodies else []

    def _classify_bodies(self, *_args):
        return self.bodies

    def _infer_outputs(self, *_args):
        self.calls += 1
        female, male = next(self.views)
        return {"prob": np.asarray([female, male], dtype=np.float32),
                "age_conv3": np.asarray([.30], dtype=np.float32)}


class ConservativeMaleFilterTests(unittest.TestCase):
    def excluded(self, recognition):
        return ExecutionManager._male_avatar_filter_excludes(
            {"exclude_male_avatar": True},
            {"person_recognition": normalize_recognition(recognition).as_screening()},
        )

    def test_two_clear_consistent_views_use_lower_score_and_inclusive_boundary(self):
        classifier = AvatarClassifier([(.05, .95), (.45, .55)])
        result = classifier.classify(b"avatar")
        self.assertEqual("male", result.category)
        self.assertEqual(2, classifier.calls)
        self.assertAlmostEqual(.55, result.confidence)
        self.assertTrue(result.male_exclusion_confirmed)
        self.assertTrue(self.excluded(result))

    def test_strong_first_score_cannot_override_female_ambiguous_or_invalid_second_view(self):
        for second in [(.8, .2), (.46, .54), (float("nan"), .99), (0, 2)]:
            with self.subTest(second=second):
                result = AvatarClassifier([(.01, .99), second]).classify(b"avatar")
                self.assertEqual("unknown", result.category)
                self.assertFalse(self.excluded(result))

    def test_low_quality_small_and_weak_detection_faces_are_retained(self):
        textured = AvatarClassifier([]).image
        for image, face in [
            (Image.new("RGB", (96, 96), "white"), (8, 8, 88, 88, .95)),
            (textured.filter(ImageFilter.GaussianBlur(5)), (8, 8, 88, 88, .95)),
            (textured, (8, 8, 28, 28, .99)),
            (textured, (8, 8, 88, 88, .7)),
        ]:
            with self.subTest(face=face, image=image):
                result = AvatarClassifier([(.01, .99)] * 2, image=image, faces=[face]).classify(b"avatar")
                self.assertEqual("unknown", result.category)
                self.assertFalse(self.excluded(result))

    def test_female_mixed_and_unresolved_group_members_are_retained(self):
        female = AvatarClassifier([(.51, .49)]).classify(b"avatar")
        self.assertEqual("female", female.category)
        self.assertFalse(self.excluded(female))
        faces = [(8, 8, 48, 88, .95), (48, 8, 88, 88, .95)]
        for rest, category in [([(.9, .1)], "couple"), ([ (.46, .54)] * 2, "unknown")]:
            result = AvatarClassifier([(.01, .99)] * 2 + rest, faces=faces).classify(b"avatar")
            self.assertEqual(category, result.category)
            self.assertFalse(self.excluded(result))

    def test_body_only_male_score_cannot_exclude(self):
        result = AvatarClassifier([], faces=[], bodies=[("male", .999)]).classify(b"avatar")
        self.assertEqual("unknown", result.category)
        self.assertEqual("male_body_without_clear_face", result.reason)
        self.assertFalse(self.excluded(result))

    def test_old_or_malformed_confirmation_and_non_avatar_sources_cannot_exclude(self):
        base = {"category": "male", "confidence": .99, "checked": True, "source": "local_openvino"}
        for flag in [None, False, "true", 1]:
            self.assertFalse(self.excluded({**base, "male_exclusion_confirmed": flag}))
        for source in ["local_openvino_body_fallback", "local_openvino_multi_image"]:
            self.assertFalse(self.excluded({**base, "source": source, "male_exclusion_confirmed": True}))


if __name__ == "__main__":
    unittest.main()
