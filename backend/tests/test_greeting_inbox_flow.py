from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.playwright_worker import (
    PlaywrightWorker,
    WorkerExecutionError,
    direct_recipient_text_matches,
)


class GreetingInboxFlowTestCase(unittest.TestCase):
    def test_recipient_match_requires_profile_href_or_explicit_handle(self) -> None:
        self.assertTrue(
            direct_recipient_text_matches(
                "Sample Recipient\n@sample_recipient01\nActive now", "sample_recipient01"
            )
        )
        self.assertFalse(
            direct_recipient_text_matches(
                "Sample Recipient Fan\n@sample_recipient01_fan", "sample_recipient01"
            )
        )
        self.assertFalse(
            direct_recipient_text_matches("sample_recipient01 fan club", "sample_recipient01")
        )
        self.assertFalse(
            direct_recipient_text_matches(
                "sample_recipient01\n@unrelated_handle\nActive now",
                "sample_recipient01",
            ),
            "a colliding display name must not be accepted as the target handle",
        )
        self.assertTrue(
            direct_recipient_text_matches(
                "Sample Recipient\nActive now",
                "sample_recipient01",
                profile_hrefs=("https://www.instagram.com/sample_recipient01/",),
            )
        )

    def test_greeting_uses_only_the_inbox_search_flow(self) -> None:
        greet_source = inspect.getsource(PlaywrightWorker.greet)
        inbox_source = inspect.getsource(
            PlaywrightWorker._open_direct_thread_from_inbox_search
        )
        search_source = inspect.getsource(PlaywrightWorker._direct_inbox_search_inputs)

        self.assertIn("/direct/inbox/", greet_source)
        self.assertIn("_open_direct_thread_from_inbox_search", greet_source)
        self.assertNotIn("/direct/new/", greet_source + inbox_source)
        self.assertNotIn("chat_labels", inbox_source)
        self.assertNotIn("下一步", inbox_source)
        self.assertIn("_visible_outside_dialog", inbox_source)
        self.assertIn("direct_inbox_result_text_matches", inbox_source)
        self.assertIn("_direct_thread_matches_recipient", inbox_source)
        self.assertNotRegex(search_source, r"(?m)^\s*'input\[")


class GreetingRecipientSafetyTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_plain_div_button_header_rejects_bare_display_name_collision(self) -> None:
        class LocatorGroup:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class HeaderNode:
            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, expression: str) -> bool:
                del expression
                return False

            async def get_attribute(self, name: str) -> str | None:
                if name == "role":
                    return "button"
                return None

            async def bounding_box(self) -> dict[str, float]:
                return {"x": 560, "y": 70, "width": 320, "height": 72}

            async def inner_text(self, **_kwargs: object) -> str:
                return "sample_match01"

        class Composer:
            async def bounding_box(self) -> dict[str, float]:
                return {"x": 500, "y": 700, "width": 600, "height": 40}

        class FakePage:
            def __init__(self) -> None:
                self.header = HeaderNode()

            def locator(self, selector: str) -> LocatorGroup:
                if "a[href]" in selector:
                    return LocatorGroup([])
                if "role" in selector and "button" in selector:
                    return LocatorGroup([self.header])
                return LocatorGroup([])

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = FakePage()

        self.assertFalse(
            await worker._direct_thread_matches_recipient("sample_match01", Composer())
        )

    async def test_plain_div_button_header_accepts_explicit_at_handle(self) -> None:
        class LocatorGroup:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class HeaderNode:
            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, expression: str) -> bool:
                del expression
                return False

            async def get_attribute(self, name: str) -> str | None:
                if name == "role":
                    return "button"
                return None

            async def bounding_box(self) -> dict[str, float]:
                return {"x": 560, "y": 70, "width": 320, "height": 72}

            async def inner_text(self, **_kwargs: object) -> str:
                return "Sample Match\n@sample_match01"

        class Composer:
            async def bounding_box(self) -> dict[str, float]:
                return {"x": 500, "y": 700, "width": 600, "height": 40}

        class FakePage:
            def __init__(self) -> None:
                self.header = HeaderNode()

            def locator(self, selector: str) -> LocatorGroup:
                if "a[href]" in selector:
                    return LocatorGroup([])
                if "role" in selector and "button" in selector:
                    return LocatorGroup([self.header])
                return LocatorGroup([])

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = FakePage()

        self.assertTrue(
            await worker._direct_thread_matches_recipient("sample_match01", Composer())
        )

    async def test_left_column_body_text_and_display_name_collision_are_rejected(self) -> None:
        class LocatorGroup:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class TextNode:
            def __init__(
                self,
                text: str,
                box: dict[str, float],
                *,
                role: str | None,
            ) -> None:
                self.text = text
                self.box = box
                self.role = role

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, expression: str) -> bool:
                del expression
                return False

            async def get_attribute(self, name: str) -> str | None:
                return self.role if name == "role" else None

            async def bounding_box(self) -> dict[str, float]:
                return self.box

            async def inner_text(self, **_kwargs: object) -> str:
                return self.text

        class Composer:
            async def bounding_box(self) -> dict[str, float]:
                return {"x": 500, "y": 700, "width": 600, "height": 40}

        left_exact = TextNode(
            "Sample Match\nsample_match01",
            {"x": 40, "y": 90, "width": 390, "height": 72},
            role="button",
        )
        wrong_right_header = TextNode(
            "sample_match01\nsample_match0104",
            {"x": 560, "y": 70, "width": 320, "height": 72},
            role="button",
        )
        right_body_mention = TextNode(
            "Earlier message mentions sample_match01",
            {"x": 600, "y": 350, "width": 360, "height": 40},
            role=None,
        )

        class FakePage:
            def locator(self, selector: str) -> LocatorGroup:
                if "a[href]" in selector:
                    return LocatorGroup([])
                if "main button" in selector or "main div" in selector:
                    return LocatorGroup(
                        [left_exact, wrong_right_header, right_body_mention]
                    )
                return LocatorGroup([])

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = FakePage()

        self.assertFalse(
            await worker._direct_thread_matches_recipient("sample_match01", Composer())
        )

    async def test_thread_verifier_rejects_left_result_and_accepts_right_header(self) -> None:
        class LocatorGroup:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class ProfileLink:
            def __init__(self, href: str, box: dict[str, float]) -> None:
                self.href = href
                self.box = box

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _expression: str) -> bool:
                return False

            async def get_attribute(self, name: str) -> str | None:
                return self.href if name == "href" else None

            async def bounding_box(self) -> dict[str, float]:
                return self.box

        class Composer:
            async def bounding_box(self) -> dict[str, float]:
                return {"x": 500, "y": 700, "width": 600, "height": 40}

        class FakePage:
            def __init__(self, links: list[ProfileLink]) -> None:
                self.links = links

            def locator(self, selector: str) -> LocatorGroup:
                if "a[href]" in selector:
                    return LocatorGroup(self.links)
                return LocatorGroup([])

        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        left_search_link = ProfileLink(
            "/sample_recipient01/", {"x": 80, "y": 100, "width": 180, "height": 40}
        )
        worker.page = FakePage([left_search_link])
        self.assertFalse(
            await worker._direct_thread_matches_recipient("sample_recipient01", Composer())
        )

        right_header_link = ProfileLink(
            "/sample_recipient01/", {"x": 560, "y": 80, "width": 180, "height": 40}
        )
        worker.page = FakePage([left_search_link, right_header_link])
        self.assertTrue(
            await worker._direct_thread_matches_recipient("sample_recipient01", Composer())
        )

    async def test_recipient_mismatch_never_reaches_send(self) -> None:
        class LocatorGroup:
            def __init__(self, nodes: list[object]) -> None:
                self.nodes = nodes

            @property
            def first(self) -> object:
                return self.nodes[0]

            @property
            def last(self) -> object:
                return self.nodes[-1]

            async def count(self) -> int:
                return len(self.nodes)

            def nth(self, index: int) -> object:
                return self.nodes[index]

        class SearchInput:
            def __init__(self) -> None:
                self.value = ""

            async def wait_for(self, **_kwargs: object) -> None:
                return None

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _expression: str) -> bool:
                return False

            async def click(self, **_kwargs: object) -> None:
                return None

            async def fill(self, value: str) -> None:
                self.value = value

            async def input_value(self) -> str:
                return self.value

            async def bounding_box(self) -> dict[str, float]:
                return {"x": 40, "y": 50, "width": 390, "height": 40}

        class RecipientRow:
            def __init__(self, page: "FakePage") -> None:
                self.page = page
                self.clicked = False

            async def is_visible(self) -> bool:
                return True

            async def evaluate(self, _expression: str) -> bool:
                return False

            async def inner_text(self, **_kwargs: object) -> str:
                return "Sample Recipient\nsample_recipient01\nA colliding display name"

            async def get_attribute(self, name: str) -> str | None:
                del name
                return None

            async def bounding_box(self) -> dict[str, float]:
                return {"x": 40, "y": 110, "width": 390, "height": 72}

            async def click(self, **_kwargs: object) -> None:
                self.clicked = True
                self.page.url = "https://www.instagram.com/direct/t/wrong-thread/"

        class Composer:
            async def count(self) -> int:
                return 1

            async def is_visible(self) -> bool:
                return True

            async def is_editable(self) -> bool:
                return True

            async def evaluate(self, expression: str) -> bool:
                del expression
                return False

            async def get_attribute(self, name: str) -> None:
                del name
                return None

            async def bounding_box(self) -> dict[str, float]:
                return {"x": 500, "y": 700, "width": 600, "height": 40}

        class FakePage:
            def __init__(self) -> None:
                self.url = "https://www.instagram.com/direct/inbox/"
                self.search = SearchInput()
                self.composer = Composer()
                self.row = RecipientRow(self)

            async def goto(self, url: str, **_kwargs: object) -> None:
                self.url = url

            def locator(self, selector: str) -> LocatorGroup:
                if "textarea" in selector or "contenteditable" in selector:
                    return LocatorGroup([self.composer])
                if selector.startswith("main a, main button"):
                    return LocatorGroup([self.row])
                return LocatorGroup([self.search])

        class SafetyWorker(PlaywrightWorker):
            def __init__(self) -> None:
                super().__init__(object())  # type: ignore[arg-type]
                self.page = FakePage()
                self.send_called = False
                self.recipient_checks = 0

            async def _ensure_window_surface_stable(self) -> None:
                return None

            async def _wait_for_profile_surface(self) -> None:
                return None

            async def _guard(self) -> None:
                return None

            async def _direct_thread_matches_recipient(
                self, username_norm: str, composer: object
            ) -> bool:
                del username_norm, composer
                self.recipient_checks += 1
                return False

            async def _send_and_confirm_greeting(
                self, composer: object, message: str, **_kwargs: object
            ) -> str:
                del composer, message
                self.send_called = True
                return "should-not-send"

        async def no_sleep(_seconds: float) -> None:
            return None

        worker = SafetyWorker()
        with patch("app.playwright_worker.asyncio.sleep", new=no_sleep):
            with self.assertRaises(WorkerExecutionError) as raised:
                await worker.greet("@sample_recipient01", "hello")

        self.assertTrue(worker.page.row.clicked)
        self.assertGreater(worker.recipient_checks, 0)
        self.assertEqual("instagram_direct_recipient_mismatch", raised.exception.code)
        self.assertFalse(worker.send_called)


if __name__ == "__main__":
    unittest.main()
