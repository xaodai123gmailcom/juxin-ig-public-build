from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.playwright_worker import (
    PlaywrightWorker,
    WorkerExecutionError,
    direct_inbox_result_text_matches,
)


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
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.value = ""

    async def wait_for(self, **_kwargs: object) -> None:
        return None

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, _expression: str) -> bool:
        return False

    async def click(self, **_kwargs: object) -> None:
        self.events.append("search_click")

    async def fill(self, value: str) -> None:
        self.value = value
        self.events.append(f"fill:{value}")

    async def input_value(self) -> str:
        return self.value

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 40, "y": 50, "width": 390, "height": 40}


class ProfileLink:
    def __init__(
        self,
        href: str,
        events: list[str],
        name: str,
        parent: "ResultRow",
    ) -> None:
        self.href = href
        self.events = events
        self.name = name
        self.parent = parent
        self.click_count = 0

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, _expression: str) -> bool:
        return False

    async def inner_text(self, **_kwargs: object) -> str:
        return self.name

    async def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 48, "y": self.parent.y + 8, "width": 48, "height": 48}

    def locator(self, selector: str) -> LocatorGroup:
        if selector.startswith("xpath=ancestor::"):
            return LocatorGroup([self.parent])
        return LocatorGroup([])

    async def click(self, **_kwargs: object) -> None:
        self.click_count += 1
        self.events.append(f"profile_link_click:{self.name}")


class ResultRow:
    def __init__(
        self,
        page: "FakePage",
        *,
        text: str,
        name: str,
        y: float,
        profile_href: str | None = None,
        change_url: bool = False,
    ) -> None:
        self.page = page
        self.text = text
        self.name = name
        self.y = y
        self.change_url = change_url
        self.click_count = 0
        self.profile_link = (
            ProfileLink(profile_href, page.events, name, self)
            if profile_href
            else None
        )

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, _expression: str) -> bool:
        return False

    async def inner_text(self, **_kwargs: object) -> str:
        return self.text

    async def get_attribute(self, _name: str) -> None:
        return None

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 40, "y": self.y, "width": 390, "height": 72}

    def locator(self, selector: str) -> LocatorGroup:
        if "a" in selector and "href" in selector and self.profile_link is not None:
            return LocatorGroup([self.profile_link])
        return LocatorGroup([])

    async def click(self, **_kwargs: object) -> None:
        self.click_count += 1
        self.page.events.append(f"row_click:{self.name}")
        self.page.thread_opened = True
        if self.change_url:
            self.page.url = "https://www.instagram.com/direct/t/thread-id/"


class Composer:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.text = ""
        self.fill_calls: list[str] = []
        self.press_calls: list[str] = []
        self.detached = False

    async def count(self) -> int:
        return 1 if self.page.thread_opened and not self.detached else 0

    async def is_visible(self) -> bool:
        return self.page.thread_opened and not self.detached

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 500, "y": 700, "width": 650, "height": 40}

    async def evaluate(self, expression: str) -> str | bool:
        if "closest" in expression:
            return False
        if self.detached:
            raise RuntimeError("composer was replaced")
        return self.text

    async def fill(self, value: str) -> None:
        self.fill_calls.append(value)
        self.page.events.append(f"composer_fill:{value}")
        self.text = value

    async def click(self, **_kwargs: object) -> None:
        self.page.events.append("composer_click")

    async def press(self, key: str) -> None:
        self.press_calls.append(key)
        self.page.events.append(f"composer_press:{key}")
        if key == "Enter":
            self.page.transcript.append(self.text)
            self.text = ""


class FakePage:
    def __init__(self) -> None:
        self.url = "https://www.instagram.com/direct/inbox/"
        self.events: list[str] = []
        self.search = SearchInput(self.events)
        self.thread_opened = False
        self.composer = Composer(self)
        self.rows: list[ResultRow] = []
        self.transcript: list[str] = []

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url
        self.events.append(f"goto:{url}")

    async def evaluate(self, _expression: str, expected: str) -> int:
        return sum(1 for item in self.transcript if item == expected)

    def locator(self, selector: str) -> LocatorGroup:
        if "input[" in selector and (
            "search" in selector.casefold()
            or "搜索" in selector
            or "搜尋" in selector
            or "buscar" in selector.casefold()
        ):
            return LocatorGroup([self.search])
        if "textarea" in selector or "contenteditable" in selector:
            return LocatorGroup([self.composer])
        if any(
            marker in selector
            for marker in (
                'role="button"',
                "role='button'",
                'role="option"',
                "role='option'",
                'role="listitem"',
                "role='listitem'",
                "main a",
            )
        ):
            candidates: list[object] = []
            for row in self.rows:
                candidates.append(row)
                if row.profile_link is not None:
                    candidates.append(row.profile_link)
            return LocatorGroup(candidates)
        return LocatorGroup([])


class FlowWorker(PlaywrightWorker):
    def __init__(self, page: FakePage, *, recipient_matches: bool = True) -> None:
        super().__init__(object())  # type: ignore[arg-type]
        self.page = page
        self.recipient_matches = recipient_matches
        self.verification_count = 0
        self.verification_saw_visible_composer = False

    async def _guard(self) -> None:
        return None

    async def _ensure_window_surface_stable(self) -> None:
        return None

    async def _wait_for_profile_surface(self) -> None:
        return None

    async def _direct_send_control(self, composer: object | None = None) -> None:
        del composer
        return None

    async def _direct_thread_matches_recipient(
        self, username_norm: str, composer: object
    ) -> bool:
        self.verification_count += 1
        self.page.events.append(f"verify:{username_norm}")
        self.verification_saw_visible_composer = bool(
            await composer.is_visible()  # type: ignore[attr-defined]
        )
        return self.recipient_matches


class DirectInboxResultTextTestCase(unittest.TestCase):
    def test_synthetic_bare_username_line_matches_without_href(self) -> None:
        self.assertTrue(
            direct_inbox_result_text_matches(
                "Sample Match\nsample_match01\nSynthetic profile bio for matching tests",
                "sample_match01",
            )
        )

    def test_display_name_collision_does_not_match_a_different_handle(self) -> None:
        self.assertFalse(
            direct_inbox_result_text_matches(
                "sample_match01\nsample_match0104\nFollow @sample_match01 for more",
                "sample_match01",
            )
        )


class DirectInboxFirstResultFlowTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_missing_search_geometry_never_selects_a_main_control(self) -> None:
        class SearchWithoutGeometry(SearchInput):
            async def bounding_box(self) -> None:
                return None

        page = FakePage()
        page.search = SearchWithoutGeometry(page.events)
        first = ResultRow(
            page,
            text="Sample Match\nsample_match01\nSynthetic profile bio for matching tests",
            name="exact-but-unscoped",
            y=110,
        )
        page.rows = [first]
        worker = FlowWorker(page)

        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._open_direct_thread_from_inbox_search(
                "sample_match01",
                "@sample_match01",
                recipient_attempts=1,
                thread_attempts=1,
                poll_interval=0,
            )

        self.assertEqual(
            "instagram_direct_inbox_recipient_not_found", raised.exception.code
        )
        self.assertEqual(0, first.click_count)
        self.assertEqual(0, worker.verification_count)

    async def test_first_mismatch_never_clicks_a_later_exact_result(self) -> None:
        page = FakePage()
        first = ResultRow(
            page,
            text="sample_match01\nsample_match0104\nAnother account",
            name="wrong-first",
            y=110,
        )
        later_exact = ResultRow(
            page,
            text="Sample Match\nsample_match01\nSynthetic profile bio for matching tests",
            name="later-exact",
            y=190,
        )
        page.rows = [first, later_exact]
        worker = FlowWorker(page)

        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._open_direct_thread_from_inbox_search(
                "sample_match01",
                "@sample_match01",
                recipient_attempts=1,
                thread_attempts=1,
                poll_interval=0,
            )

        self.assertEqual(
            "instagram_direct_inbox_recipient_not_found", raised.exception.code
        )
        self.assertEqual(0, first.click_count)
        self.assertEqual(0, later_exact.click_count)
        self.assertEqual(0, worker.verification_count)
        self.assertFalse(page.thread_opened)
        self.assertEqual("https://www.instagram.com/direct/inbox/", page.url)

    async def test_exact_first_result_clicks_outer_row_not_profile_link(self) -> None:
        page = FakePage()
        first = ResultRow(
            page,
            text="Sample Match\nsample_match01\nSynthetic profile bio for matching tests",
            name="exact-first",
            y=110,
            profile_href="/sample_match01/",
            change_url=True,
        )
        later = ResultRow(
            page,
            text="Sample Match Star\nsample_match0104\nAnother account",
            name="later-similar",
            y=190,
        )
        page.rows = [first, later]
        worker = FlowWorker(page)

        self.assertIs(
            first,
            await worker._direct_inbox_result_click_target(first.profile_link),
        )

        composer = await worker._open_direct_thread_from_inbox_search(
            "sample_match01",
            "@sample_match01",
            recipient_attempts=1,
            thread_attempts=1,
            poll_interval=0,
        )

        self.assertIs(page.composer, composer)
        self.assertEqual(1, first.click_count)
        self.assertIsNotNone(first.profile_link)
        self.assertEqual(0, first.profile_link.click_count)  # type: ignore[union-attr]
        self.assertEqual(0, later.click_count)
        self.assertLess(
            page.events.index("fill:sample_match01"),
            page.events.index("row_click:exact-first"),
        )
        self.assertLess(
            page.events.index("row_click:exact-first"),
            page.events.index("verify:sample_match01"),
        )

    async def test_visible_composer_is_verified_even_when_url_does_not_change(self) -> None:
        page = FakePage()
        first = ResultRow(
            page,
            text="Sample Match\nsample_match01\nSynthetic profile bio for matching tests",
            name="exact-first",
            y=110,
            change_url=False,
        )
        page.rows = [first]
        worker = FlowWorker(page, recipient_matches=True)
        original_url = page.url

        composer = await worker._open_direct_thread_from_inbox_search(
            "sample_match01",
            "@sample_match01",
            recipient_attempts=1,
            thread_attempts=1,
            poll_interval=0,
        )

        self.assertIs(page.composer, composer)
        self.assertEqual(original_url, page.url)
        self.assertEqual(1, first.click_count)
        self.assertEqual(1, worker.verification_count)
        self.assertTrue(worker.verification_saw_visible_composer)

    async def test_greet_runs_first_result_through_exact_fill_enter_and_bubble(self) -> None:
        page = FakePage()
        first = ResultRow(
            page,
            text="Sample Match\nsample_match01\nSynthetic profile bio for matching tests",
            name="exact-first",
            y=110,
            profile_href="/sample_match01/",
            change_url=False,
        )
        page.rows = [first]
        worker = FlowWorker(page, recipient_matches=True)
        message = "你好，Nice to meet you 🙂"

        outcome = await worker.greet("@sample_match01", message)

        self.assertEqual("confirmed", outcome.status)
        self.assertEqual("sample_match01", outcome.target)
        self.assertEqual(
            "message_visible_in_direct_thread:enter_key",
            outcome.visible_confirmation,
        )
        self.assertEqual([message], page.composer.fill_calls)
        self.assertEqual(["Enter"], page.composer.press_calls)
        self.assertEqual([message], page.transcript)
        self.assertEqual("", page.composer.text)
        self.assertLess(
            page.events.index("row_click:exact-first"),
            page.events.index(f"composer_fill:{message}"),
        )
        self.assertLess(
            page.events.index(f"composer_fill:{message}"),
            page.events.index("composer_press:Enter"),
        )


if __name__ == "__main__":
    unittest.main()
