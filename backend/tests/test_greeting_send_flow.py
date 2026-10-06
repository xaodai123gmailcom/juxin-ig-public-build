from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.action_manager import ActionCampaignManager
from app.database import Database
from app.playwright_worker import (
    ActionOutcome,
    PlaywrightWorker,
    WorkerExecutionError,
    normalize_direct_message_text,
)
from app.service import CoreService


class _LocatorGroup:
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


class _Keyboard:
    def __init__(self, page: "_GreetingPage") -> None:
        self.page = page
        self.insertions: list[str] = []

    async def insert_text(self, value: str) -> None:
        self.insertions.append(value)
        self.page.events.append(f"insert_text:{value}")
        composer = self.page.active_composer
        if composer is None or composer.detached:
            raise RuntimeError("no focused composer")
        composer.text += value


class _GreetingPage:
    def __init__(self, *, send_mode: str = "success") -> None:
        self.send_mode = send_mode
        self.events: list[str] = []
        self.transcript: list[tuple[str, str]] = []
        self.composers: list[_Composer] = []
        self.controls: list[_SendCandidate] = []
        self.active_composer: _Composer | None = None
        self.keyboard = _Keyboard(self)

    def locator(self, selector: str) -> _LocatorGroup:
        if "textarea" in selector or "contenteditable" in selector or "lexical" in selector:
            return _LocatorGroup(list(self.composers))
        if "main button" in selector or "role=\"button\"" in selector:
            return _LocatorGroup(list(self.controls))
        return _LocatorGroup([])

    async def submit(self, composer: "_Composer", trigger: str) -> None:
        self.events.append(f"submit:{trigger}")
        message = composer.text
        if self.send_mode == "success":
            self.transcript.append(("right", message))
            composer.text = ""
            return
        if self.send_mode == "dom_replace":
            self.transcript.append(("right", message))
            composer.detached = True
            replacement = _Composer(self, kind=composer.kind, text="")
            self.composers.append(replacement)
            self.active_composer = replacement
            return
        if self.send_mode == "empty_without_bubble":
            composer.text = ""
            return
        if self.send_mode == "bubble_without_empty":
            self.transcript.append(("right", message))
            return
        if self.send_mode == "no_effect":
            return
        if self.send_mode == "exception_after_trigger":
            raise RuntimeError("connection lost after send trigger")
        raise AssertionError(f"unsupported send mode: {self.send_mode}")


class _Composer:
    def __init__(
        self,
        page: _GreetingPage,
        *,
        kind: str,
        text: str = "",
        visible: bool = True,
        inside_dialog: bool = False,
        fill_mode: str = "normal",
        box: dict[str, float] | None = None,
        after_fill: Callable[[], None] | None = None,
    ) -> None:
        self.page = page
        self.kind = kind
        self.text = text
        self.visible = visible
        self.inside_dialog = inside_dialog
        self.fill_mode = fill_mode
        self.box = box or {"x": 500, "y": 700, "width": 650, "height": 40}
        self.after_fill = after_fill
        self.detached = False
        self.fill_calls: list[str] = []
        self.click_count = 0
        self.press_calls: list[str] = []
        page.composers.append(self)
        if visible and not inside_dialog:
            page.active_composer = self

    async def count(self) -> int:
        return 0 if self.detached else 1

    async def is_visible(self) -> bool:
        return self.visible and not self.detached

    async def is_enabled(self) -> bool:
        return True

    async def is_editable(self) -> bool:
        return self.visible and not self.detached

    async def bounding_box(self) -> dict[str, float] | None:
        return None if self.detached else self.box

    async def get_attribute(self, name: str) -> str | None:
        attributes = {
            "contenteditable": (
                "plaintext-only"
                if self.kind == "plaintext"
                else "true" if self.kind == "lexical" else None
            ),
            "role": "textbox" if self.kind == "lexical" else None,
            "data-lexical-editor": "true" if self.kind == "lexical" else None,
            "aria-label": "消息" if self.kind != "textarea" else None,
            "placeholder": "发消息……" if self.kind == "textarea" else None,
            "disabled": None,
        }
        return attributes.get(name)

    async def evaluate(self, expression: str) -> str | bool:
        if "closest" in expression:
            return self.inside_dialog
        if self.detached:
            raise RuntimeError("composer was replaced")
        return self.text

    async def fill(self, value: str) -> None:
        self.fill_calls.append(value)
        self.page.events.append(f"fill:{value}")
        if self.fill_mode == "normal":
            self.text = value
        elif self.fill_mode == "noop":
            pass
        elif self.fill_mode == "truncate":
            self.text = value[:-1]
        elif self.fill_mode == "collapse_whitespace":
            self.text = " ".join(value.split())
        else:
            raise AssertionError(f"unsupported fill mode: {self.fill_mode}")
        if self.after_fill is not None:
            self.after_fill()

    async def click(self, **_kwargs: object) -> None:
        self.click_count += 1
        self.page.active_composer = self
        self.page.events.append("composer_click")

    async def press(self, key: str) -> None:
        self.press_calls.append(key)
        self.page.events.append(f"press:{key}")
        if key == "Enter":
            await self.page.submit(self, "enter")


class _DomComposer(_Composer):
    """Composer node whose fake page applies the production CSS selector."""

    def __init__(
        self,
        page: _GreetingPage,
        *,
        dom_kind: str,
        disabled: bool = False,
        readonly: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(page, kind=dom_kind, **kwargs)
        self.dom_kind = dom_kind
        self.disabled = disabled
        self.readonly = readonly

    def matches_selector(self, selector: str) -> bool:
        marker_by_kind = {
            "textarea": "textarea",
            "contenteditable-no-role": '[contenteditable="true"]',
            "plaintext": '[contenteditable="plaintext-only"]',
            "lexical": '[data-lexical-editor="true"]',
            "slate": '[data-slate-editor="true"]',
        }
        return marker_by_kind[self.dom_kind] in selector

    async def is_enabled(self) -> bool:
        return not self.disabled

    async def is_editable(self) -> bool:
        return (
            self.visible
            and not self.detached
            and not self.disabled
            and not self.readonly
        )

    async def get_attribute(self, name: str) -> str | None:
        attributes = {
            "contenteditable": (
                "true"
                if self.dom_kind == "contenteditable-no-role"
                else "plaintext-only" if self.dom_kind == "plaintext" else None
            ),
            "role": "textbox" if self.dom_kind == "lexical" else None,
            "data-lexical-editor": "true" if self.dom_kind == "lexical" else None,
            "data-slate-editor": "true" if self.dom_kind == "slate" else None,
            "aria-label": "消息",
            "placeholder": "发消息……" if self.dom_kind == "textarea" else None,
            "disabled": "" if self.disabled else None,
            "aria-disabled": "true" if self.disabled else None,
            "readonly": "" if self.readonly else None,
        }
        return attributes.get(name)


class _SendControl:
    def __init__(self, page: _GreetingPage) -> None:
        self.page = page
        self.click_count = 0

    async def click(self, **_kwargs: object) -> None:
        self.click_count += 1
        composer = self.page.active_composer
        if composer is None:
            raise RuntimeError("missing composer")
        await self.page.submit(composer, "button")


class _SendCandidate:
    def __init__(
        self,
        page: _GreetingPage,
        *,
        text: str,
        box: dict[str, float],
        aria_label: str | None = None,
        visible: bool = True,
        enabled: bool = True,
        inside_dialog: bool = False,
    ) -> None:
        self.page = page
        self.text = text
        self.box = box
        self.aria_label = aria_label
        self.visible = visible
        self.enabled = enabled
        self.inside_dialog = inside_dialog
        self.click_count = 0
        page.controls.append(self)

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def evaluate(self, expression: str) -> bool:
        del expression
        return self.inside_dialog

    async def get_attribute(self, name: str) -> str | None:
        if name == "aria-label":
            return self.aria_label
        if name == "aria-disabled":
            return "true" if not self.enabled else None
        if name == "disabled":
            return "" if not self.enabled else None
        return None

    async def bounding_box(self) -> dict[str, float]:
        return self.box

    async def inner_text(self, **_kwargs: object) -> str:
        return self.text

    async def click(self, **_kwargs: object) -> None:
        self.click_count += 1


class _SubmittingSendCandidate(_SendCandidate):
    async def click(self, **_kwargs: object) -> None:
        self.click_count += 1
        composer = self.page.active_composer
        if composer is None:
            raise RuntimeError("missing focused composer")
        await self.page.submit(composer, "button")


class _SelectorAwareGreetingPage(_GreetingPage):
    def locator(self, selector: str) -> _LocatorGroup:
        if any(
            marker in selector
            for marker in (
                "textarea",
                "contenteditable",
                "data-lexical-editor",
                "data-slate-editor",
                'role="textbox"',
            )
        ):
            return _LocatorGroup(
                [
                    node
                    for node in self.composers
                    if isinstance(node, _DomComposer)
                    and node.matches_selector(selector)
                ]
            )
        if "main button" in selector or 'role="button"' in selector:
            return _LocatorGroup(list(self.controls))
        return _LocatorGroup([])


class _GreetingWorker(PlaywrightWorker):
    def __init__(
        self,
        page: _GreetingPage,
        *,
        send_control: _SendControl | None = None,
        transcript_error: bool = False,
        guard_error_after_trigger: bool = False,
    ) -> None:
        super().__init__(object())  # type: ignore[arg-type]
        self.page = page
        self.send_control = send_control
        self.transcript_error = transcript_error
        self.guard_error_after_trigger = guard_error_after_trigger
        self.recipient_matches = True
        self.recipient_checks = 0

    async def _guard(self) -> None:
        if self.guard_error_after_trigger and any(
            event.startswith("submit:") for event in self.page.events
        ):
            raise WorkerExecutionError(
                "Direct page disconnected after send trigger",
                reason="instagram_network_unavailable",
                pause_required=True,
            )
        return None

    async def _direct_send_control(
        self, composer: object | None = None
    ) -> _SendControl | None:
        del composer
        return self.send_control

    async def _visible_transcript_message_count(self, message: str) -> int:
        if self.transcript_error:
            raise RuntimeError("transcript cannot be inspected")
        return sum(
            1
            for column, visible_message in self.page.transcript
            if column == "right" and visible_message == message
        )

    async def _direct_thread_matches_recipient(
        self, username_norm: str, composer: object
    ) -> bool:
        del composer
        self.recipient_checks += 1
        self.page.events.append(f"recipient_check:{username_norm}")
        return self.recipient_matches

    async def _visible_direct_composer(
        self,
        *,
        reference: object | None = None,
        maximum: int = 40,
    ) -> _Composer | None:
        del reference, maximum
        composer = self.page.active_composer
        if composer is None or not await composer.is_visible():
            return None
        return composer


class _ProductionSelectionGreetingWorker(PlaywrightWorker):
    """Use production composer/send selection while faking only remote effects."""

    def __init__(self, page: _SelectorAwareGreetingPage) -> None:
        super().__init__(object())  # type: ignore[arg-type]
        self.page = page

    async def _guard(self) -> None:
        return None

    async def _visible_transcript_message_count(self, message: str) -> int:
        return sum(
            1
            for column, visible_message in self.page.transcript
            if column == "right" and visible_message == message
        )

    async def _direct_thread_matches_recipient(
        self,
        username_norm: str,
        composer: object,
    ) -> bool:
        del username_norm, composer
        return True


class GreetingComposerSelectionTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_visible_composer_is_selected_even_when_it_is_not_last(self) -> None:
        page = _GreetingPage()
        visible = _Composer(page, kind="plaintext", visible=True)
        _Composer(page, kind="lexical", visible=False)
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = page

        selected = await worker._visible_direct_composer()

        self.assertIs(visible, selected)

    def test_selector_contract_covers_textarea_plaintext_and_lexical_editors(self) -> None:
        selector = PlaywrightWorker(object()).selectors.message_composer  # type: ignore[arg-type]
        self.assertIn("textarea", selector)
        self.assertIn("plaintext-only", selector)
        self.assertIn("data-lexical-editor", selector)
        self.assertTrue(
            all(branch.strip().startswith("main ") for branch in selector.split(","))
        )

    async def test_real_page_never_falls_back_to_an_unsafe_old_composer(self) -> None:
        page = _SelectorAwareGreetingPage()
        composer = _DomComposer(page, dom_kind="lexical")
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = page
        self.assertIs(composer, await worker._current_direct_composer(composer))

        composer.inside_dialog = True

        self.assertIsNone(await worker._current_direct_composer(composer))

    async def test_no_role_contenteditable_is_selected_by_real_selector(self) -> None:
        page = _SelectorAwareGreetingPage()
        composer = _DomComposer(page, dom_kind="contenteditable-no-role")
        self.assertIsNone(await composer.get_attribute("role"))
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = page

        selected = await worker._visible_direct_composer()

        self.assertIs(composer, selected)

    async def test_slate_is_selected_while_readonly_disabled_and_dialog_nodes_are_excluded(self) -> None:
        page = _SelectorAwareGreetingPage()
        readonly = _DomComposer(
            page,
            dom_kind="textarea",
            readonly=True,
            box={"x": 500, "y": 760, "width": 650, "height": 40},
        )
        disabled = _DomComposer(
            page,
            dom_kind="textarea",
            disabled=True,
            box={"x": 500, "y": 770, "width": 650, "height": 40},
        )
        dialog = _DomComposer(
            page,
            dom_kind="contenteditable-no-role",
            inside_dialog=True,
            box={"x": 500, "y": 780, "width": 650, "height": 40},
        )
        slate = _DomComposer(
            page,
            dom_kind="slate",
            box={"x": 500, "y": 700, "width": 650, "height": 40},
        )
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = page

        selected = await worker._visible_direct_composer()

        self.assertIs(slate, selected)
        self.assertIsNot(readonly, selected)
        self.assertIsNot(disabled, selected)
        self.assertIsNot(dialog, selected)

    async def test_send_control_is_enabled_visible_and_near_the_right_composer(self) -> None:
        page = _GreetingPage()
        _Composer(page, kind="lexical")
        left_decoy = _SendCandidate(
            page,
            text="发送",
            box={"x": 40, "y": 700, "width": 80, "height": 36},
        )
        disabled_nearby = _SendCandidate(
            page,
            text="发送",
            box={"x": 1080, "y": 700, "width": 80, "height": 36},
            enabled=False,
        )
        dialog_decoy = _SendCandidate(
            page,
            text="发送",
            box={"x": 1080, "y": 700, "width": 80, "height": 36},
            inside_dialog=True,
        )
        correct = _SendCandidate(
            page,
            text="",
            aria_label="发送消息",
            box={"x": 1080, "y": 700, "width": 80, "height": 36},
        )
        worker = PlaywrightWorker(object())  # type: ignore[arg-type]
        worker.page = page

        selected = await worker._direct_send_control()

        self.assertIs(correct, selected)
        self.assertEqual(0, left_decoy.click_count)
        self.assertEqual(0, disabled_nearby.click_count)
        self.assertEqual(0, dialog_decoy.click_count)

    async def test_real_send_control_selection_clicks_only_safe_adjacent_button(self) -> None:
        page = _SelectorAwareGreetingPage()
        composer = _DomComposer(page, dom_kind="contenteditable-no-role")
        left_decoy = _SendCandidate(
            page,
            text="发送",
            box={"x": 40, "y": 700, "width": 80, "height": 36},
        )
        disabled_decoy = _SendCandidate(
            page,
            text="发送",
            box={"x": 1080, "y": 700, "width": 80, "height": 36},
            enabled=False,
        )
        dialog_decoy = _SendCandidate(
            page,
            text="发送",
            box={"x": 1080, "y": 700, "width": 80, "height": 36},
            inside_dialog=True,
        )
        send = _SubmittingSendCandidate(
            page,
            text="",
            aria_label="发送消息",
            box={"x": 1080, "y": 700, "width": 80, "height": 36},
        )
        worker = _ProductionSelectionGreetingWorker(page)
        message = "真实选择的发送按钮"

        confirmation = await worker._send_and_confirm_greeting(
            composer,
            message,
            username_norm="sample_match01",
            poll_attempts=2,
            poll_interval=0,
        )

        self.assertEqual(
            "message_visible_in_direct_thread:send_button",
            confirmation,
        )
        self.assertEqual(1, send.click_count)
        self.assertEqual(0, left_decoy.click_count)
        self.assertEqual(0, disabled_decoy.click_count)
        self.assertEqual(0, dialog_decoy.click_count)
        self.assertEqual([("right", message)], page.transcript)
        self.assertEqual("", composer.text)


class GreetingMessageNormalizationTestCase(unittest.TestCase):
    def test_only_transport_artifacts_are_normalized(self) -> None:
        self.assertEqual(
            "第一行  双空格\n第二行 空格",
            normalize_direct_message_text(
                "第一行  双空格\r\n第二行\u00a0空格\u200b\ufeff"
            ),
        )


class GreetingComposerSendTestCase(unittest.IsolatedAsyncioTestCase):
    async def _send(
        self,
        worker: _GreetingWorker,
        composer: _Composer,
        message: str,
    ) -> str:
        return await worker._send_and_confirm_greeting(
            composer,
            message,
            username_norm="sample_match01",
            poll_attempts=2,
            poll_interval=0,
        )

    async def test_textarea_plaintext_and_lexical_fill_exactly_then_enter(self) -> None:
        message = "第一行  保留双空格\nSecond line 🙂"
        for kind in ("textarea", "plaintext", "lexical"):
            with self.subTest(kind=kind):
                page = _GreetingPage()
                composer = _Composer(page, kind=kind)
                worker = _GreetingWorker(page)

                confirmation = await self._send(worker, composer, message)

                self.assertEqual(
                    "message_visible_in_direct_thread:enter_key", confirmation
                )
                self.assertEqual([message], composer.fill_calls)
                self.assertEqual(["Enter"], composer.press_calls)
                self.assertEqual([("right", message)], page.transcript)
                self.assertEqual("", composer.text)

    async def test_existing_draft_is_replaced_by_the_verified_greeting(self) -> None:
        page = _GreetingPage()
        composer = _Composer(page, kind="lexical", text="我的未发送草稿")
        worker = _GreetingWorker(page)
        message = "新的自动招呼"

        confirmation = await self._send(worker, composer, message)

        self.assertEqual("message_visible_in_direct_thread:enter_key", confirmation)
        self.assertEqual([message], composer.fill_calls)
        self.assertEqual(["Enter"], composer.press_calls)
        self.assertEqual([], page.keyboard.insertions)
        self.assertEqual([("right", message)], page.transcript)

    async def test_visually_empty_editor_artifacts_are_not_mistaken_for_a_draft(self) -> None:
        page = _GreetingPage()
        composer = _Composer(
            page,
            kind="lexical",
            text="\n\u00a0\u200b\ufeff\n",
        )
        worker = _GreetingWorker(page)
        message = "空输入框应正常写入并发送"

        confirmation = await self._send(worker, composer, message)

        self.assertEqual("message_visible_in_direct_thread:enter_key", confirmation)
        self.assertEqual([message], composer.fill_calls)
        self.assertEqual(["Enter"], composer.press_calls)
        self.assertEqual([("right", message)], page.transcript)

    async def test_placeholder_descendant_is_removed_before_draft_detection(self) -> None:
        class PlaceholderComposer(_Composer):
            async def evaluate(self, expression: str) -> str | bool:
                if "closest" in expression:
                    return False
                if (
                    not self.text
                    and "cloneNode" in expression
                    and "data-placeholder" in expression
                ):
                    return "\n"
                return self.text

        page = _GreetingPage()
        composer = PlaceholderComposer(page, kind="lexical")
        worker = _GreetingWorker(page)
        message = "结构化占位文字不能被当成草稿"

        confirmation = await self._send(worker, composer, message)

        self.assertEqual("message_visible_in_direct_thread:enter_key", confirmation)
        self.assertEqual([message], composer.fill_calls)
        self.assertEqual(["Enter"], composer.press_calls)
        self.assertEqual([("right", message)], page.transcript)

    async def test_fill_noop_uses_insert_text_without_clipboard(self) -> None:
        page = _GreetingPage()
        composer = _Composer(page, kind="lexical", fill_mode="noop")
        worker = _GreetingWorker(page)
        message = "fill 无效时仍应输入"

        confirmation = await self._send(worker, composer, message)

        self.assertEqual("message_visible_in_direct_thread:enter_key", confirmation)
        self.assertEqual([message], composer.fill_calls)
        self.assertEqual([message], page.keyboard.insertions)
        self.assertEqual(2, composer.click_count)
        self.assertEqual(["Enter"], composer.press_calls)
        self.assertEqual([("right", message)], page.transcript)

    async def test_fill_followed_by_unknown_editor_read_never_inserts_or_sends(self) -> None:
        class ReadFailsAfterFillComposer(_Composer):
            async def evaluate(self, expression: str) -> str | bool:
                if "closest" in expression:
                    return False
                if self.fill_calls:
                    raise RuntimeError("editor state cannot be read")
                return await super().evaluate(expression)

        page = _GreetingPage()
        composer = ReadFailsAfterFillComposer(page, kind="lexical")
        worker = _GreetingWorker(page)

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, "读取失败时不能继续")

        self.assertEqual("instagram_direct_composer_not_ready", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])
        self.assertEqual([], page.keyboard.insertions)
        self.assertEqual([], composer.press_calls)
        self.assertEqual([], page.transcript)

    async def test_fill_noop_with_disappearing_editor_never_uses_keyboard_fallback(self) -> None:
        page = _GreetingPage()
        composer: _Composer

        def hide_after_fill() -> None:
            composer.visible = False

        composer = _Composer(
            page,
            kind="lexical",
            fill_mode="noop",
            after_fill=hide_after_fill,
        )
        worker = _GreetingWorker(page)

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, "没有连续空值证据不能写入")

        self.assertEqual("instagram_direct_composer_not_ready", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])
        self.assertEqual([], page.keyboard.insertions)
        self.assertEqual([], composer.press_calls)
        self.assertEqual([], page.transcript)

    async def test_partial_or_truncated_fill_never_uses_fallback_or_sends(self) -> None:
        page = _GreetingPage()
        composer = _Composer(page, kind="plaintext", fill_mode="truncate")
        worker = _GreetingWorker(page)
        message = "不能少最后一个字"

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, message)

        self.assertEqual("instagram_direct_composer_not_ready", raised.exception.code)
        self.assertEqual([message], composer.fill_calls)
        self.assertEqual([], page.keyboard.insertions)
        self.assertEqual([], composer.press_calls)
        self.assertEqual([], page.transcript)

    async def test_collapsed_multiline_or_double_space_text_is_not_sent(self) -> None:
        page = _GreetingPage()
        composer = _Composer(
            page,
            kind="lexical",
            fill_mode="collapse_whitespace",
        )
        worker = _GreetingWorker(page)
        message = "第一行  保留双空格\nSecond line"

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, message)

        self.assertEqual("instagram_direct_composer_not_ready", raised.exception.code)
        self.assertEqual([], page.keyboard.insertions)
        self.assertEqual([], composer.press_calls)
        self.assertEqual([], page.transcript)

    async def test_recipient_switch_after_fill_is_caught_before_send(self) -> None:
        page = _GreetingPage()
        worker = _GreetingWorker(page)
        composer = _Composer(
            page,
            kind="lexical",
            after_fill=lambda: setattr(worker, "recipient_matches", False),
        )
        message = "这条不能发给切换后的会话"

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, message)

        self.assertEqual("instagram_direct_recipient_mismatch", raised.exception.code)
        self.assertGreaterEqual(worker.recipient_checks, 1)
        self.assertEqual([message], composer.fill_calls)
        self.assertEqual(message, composer.text)
        self.assertEqual([], composer.press_calls)
        self.assertEqual([], page.transcript)

    async def test_switch_to_other_thread_with_same_text_draft_preserves_it(self) -> None:
        page = _GreetingPage()
        message = "两个会话恰好相同的草稿"
        send_control = _SendControl(page)

        class ThreadSwitchWorker(_GreetingWorker):
            async def _direct_send_control(
                self, composer: object | None = None
            ) -> _SendControl | None:
                del composer
                _Composer(page, kind="lexical", text=message)
                self.recipient_matches = False
                return self.send_control

        original = _Composer(page, kind="lexical")
        worker = ThreadSwitchWorker(page, send_control=send_control)

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, original, message)

        self.assertEqual("instagram_direct_recipient_mismatch", raised.exception.code)
        foreign = page.active_composer
        self.assertIsNotNone(foreign)
        self.assertIsNot(original, foreign)
        self.assertEqual(message, foreign.text)  # type: ignore[union-attr]
        self.assertEqual([], foreign.fill_calls)  # type: ignore[union-attr]
        self.assertEqual(0, send_control.click_count)
        self.assertEqual([], page.transcript)

    async def test_visible_send_button_path_requires_new_right_bubble_and_empty_input(self) -> None:
        page = _GreetingPage()
        composer = _Composer(page, kind="textarea")
        send_control = _SendControl(page)
        worker = _GreetingWorker(page, send_control=send_control)
        message = "使用发送按钮"

        confirmation = await self._send(worker, composer, message)

        self.assertEqual("message_visible_in_direct_thread:send_button", confirmation)
        self.assertEqual(1, send_control.click_count)
        self.assertEqual([], composer.press_calls)
        self.assertEqual([("right", message)], page.transcript)
        self.assertEqual("", composer.text)

    async def test_preexisting_identical_bubble_does_not_confirm_without_a_new_one(self) -> None:
        message = "重复文本"
        page = _GreetingPage(send_mode="no_effect")
        page.transcript.append(("right", message))
        composer = _Composer(page, kind="textarea")
        worker = _GreetingWorker(page)

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, message)

        self.assertEqual("instagram_action_outcome_unknown", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])
        self.assertEqual([("right", message)], page.transcript)

    async def test_empty_without_bubble_and_bubble_without_empty_are_unknown(self) -> None:
        for mode in ("empty_without_bubble", "bubble_without_empty"):
            with self.subTest(mode=mode):
                page = _GreetingPage(send_mode=mode)
                composer = _Composer(page, kind="lexical")
                worker = _GreetingWorker(page)

                with self.assertRaises(WorkerExecutionError) as raised:
                    await self._send(worker, composer, "必须双重确认")

                self.assertEqual(
                    "instagram_action_outcome_unknown", raised.exception.code
                )
                self.assertTrue(raised.exception.details["pause_required"])

    async def test_composer_dom_replacement_after_enter_still_confirms(self) -> None:
        page = _GreetingPage(send_mode="dom_replace")
        original = _Composer(page, kind="lexical")
        worker = _GreetingWorker(page)
        message = "发送后 React 替换输入框"

        confirmation = await self._send(worker, original, message)

        self.assertEqual("message_visible_in_direct_thread:enter_key", confirmation)
        self.assertTrue(original.detached)
        self.assertIsNot(original, page.active_composer)
        self.assertEqual("", page.active_composer.text)  # type: ignore[union-attr]
        self.assertEqual([("right", message)], page.transcript)

    async def test_post_trigger_exception_is_unknown_and_pauses(self) -> None:
        page = _GreetingPage(send_mode="exception_after_trigger")
        composer = _Composer(page, kind="textarea")
        worker = _GreetingWorker(page)

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, "触发后连接中断")

        self.assertEqual("instagram_action_outcome_unknown", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])

    async def test_post_trigger_cancellation_is_marked_unknown_and_propagated(self) -> None:
        class CancelAfterTriggerPage(_GreetingPage):
            async def submit(self, composer: _Composer, trigger: str) -> None:
                self.events.append(f"submit:{trigger}")
                del composer
                raise asyncio.CancelledError

        page = CancelAfterTriggerPage()
        composer = _Composer(page, kind="textarea")
        worker = _GreetingWorker(page)

        with self.assertRaises(asyncio.CancelledError) as raised:
            await self._send(worker, composer, "触发后任务被取消")

        self.assertTrue(
            getattr(
                raised.exception,
                "instagram_action_outcome_unknown",
                False,
            )
        )
        self.assertEqual(["Enter"], composer.press_calls)

    async def test_pre_trigger_transcript_read_failure_does_not_send(self) -> None:
        page = _GreetingPage()
        composer = _Composer(page, kind="textarea")
        worker = _GreetingWorker(page, transcript_error=True)

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, "基线读取失败")

        self.assertEqual("instagram_direct_chat_not_rendered", raised.exception.code)
        self.assertEqual([], composer.fill_calls)
        self.assertEqual([], composer.press_calls)
        self.assertEqual([], page.transcript)

    async def test_post_trigger_worker_error_is_always_unknown(self) -> None:
        page = _GreetingPage(send_mode="success")
        composer = _Composer(page, kind="lexical")
        worker = _GreetingWorker(page, guard_error_after_trigger=True)

        with self.assertRaises(WorkerExecutionError) as raised:
            await self._send(worker, composer, "发送后页面断开")

        self.assertEqual("instagram_action_outcome_unknown", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])
        self.assertEqual([("right", "发送后页面断开")], page.transcript)


class _ScriptedGreetingWorker:
    outcomes: dict[str, str | list[str]] = {}
    calls: list[tuple[str, str, str, str | None]] = []

    def __init__(self, _bitbrowser: object) -> None:
        self.profile_id = ""

    @classmethod
    def reset(cls, outcomes: dict[str, str | list[str]]) -> None:
        cls.outcomes = {
            profile_id: list(outcome) if isinstance(outcome, list) else outcome
            for profile_id, outcome in outcomes.items()
        }
        cls.calls = []

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        del open_if_needed
        self.profile_id = profile_id

    async def disconnect(self) -> None:
        return None

    async def execute_action(
        self,
        operation: str,
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        type(self).calls.append((self.profile_id, operation, target, message))
        configured = type(self).outcomes[self.profile_id]
        outcome = configured.pop(0) if isinstance(configured, list) else configured
        if outcome == "confirmed":
            return ActionOutcome(
                "greet", target, "confirmed", "message_visible_in_direct_thread"
            )
        if outcome == "unknown":
            raise WorkerExecutionError(
                "消息触发后无法确认",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            )
        if outcome == "composer_failed":
            raise WorkerExecutionError(
                "输入框写入失败，尚未发送",
                reason="instagram_direct_composer_not_ready",
                pause_required=False,
            )
        raise AssertionError(f"unsupported outcome: {outcome}")


class GreetingCampaignOutcomeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "greeting.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.user = self.service.register_user(
            "greeting-outcome-user", "Correct-Horse-Battery-Staple-42!"
        )
        self.manager = ActionCampaignManager(
            self.service,
            SimpleNamespace(close_profile=lambda _: {'closed': True}),
            worker_factory=_ScriptedGreetingWorker,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temporary.cleanup()

    async def test_unknown_send_pauses_campaign_and_never_enters_success_history(self) -> None:
        _ScriptedGreetingWorker.reset({"window-unknown": "unknown"})
        campaign = await self.manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="window-unknown",
            targets=["uncertain.person", "must.wait"],
            message="Hello unknown",
            messages=["Hello unknown"],
            interval="1 秒",
            limit=2,
        )

        await self.manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["unknown", "pending"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual("unknown", stored["attempts"][0]["status"])
        self.assertEqual("Hello unknown", stored["attempts"][0]["message"])
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual([], snapshot["action_success_history"])
        self.assertEqual(0, snapshot["dedupe"]["greet_successes"])
        self.assertEqual(
            [("window-unknown", "greet", "uncertain.person", "Hello unknown")],
            _ScriptedGreetingWorker.calls,
        )

    async def test_definite_pre_send_failure_pauses_then_resume_continues_next_target(self) -> None:
        _ScriptedGreetingWorker.reset(
            {"window-continue": ["composer_failed", "confirmed"]}
        )
        campaign = await self.manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="window-continue",
            targets=["composer.failed", "next.succeeds"],
            message="继续使用的话术",
            messages=["继续使用的话术"],
            interval="1 秒",
            limit=2,
        )

        await self.manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("paused", stored["status"])
        self.assertEqual(
            ["failed", "pending"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual(["failed"], [attempt["status"] for attempt in stored["attempts"]])
        self.assertEqual(
            ["composer.failed"],
            [call[2] for call in _ScriptedGreetingWorker.calls],
        )

        await self.manager.resume(self.user["id"], campaign["id"])
        await self.manager.wait(campaign["id"])

        stored = self.service.get_action_campaign(self.user["id"], campaign["id"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual(
            ["failed", "confirmed"],
            [target["status"] for target in stored["targets"]],
        )
        self.assertEqual(
            ["failed", "confirmed"],
            [attempt["status"] for attempt in stored["attempts"]],
        )
        self.assertEqual(
            ["composer.failed", "next.succeeds"],
            [call[2] for call in _ScriptedGreetingWorker.calls],
        )
        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(1, snapshot["dedupe"]["greet_successes"])
        self.assertEqual(
            ["next.succeeds"],
            [
                item["targets"][0]["username"]
                for item in snapshot["action_success_history"]
            ],
        )

    async def test_two_greeting_windows_are_isolated_and_history_keeps_exact_message(self) -> None:
        _ScriptedGreetingWorker.reset(
            {"window-success": "confirmed", "window-failed": "unknown"}
        )
        success_message = "窗口 A 的精确话术 🙂"
        success = await self.manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="window-success",
            targets=["success.person"],
            message=success_message,
            messages=[success_message],
            interval="1 秒",
            limit=1,
        )
        failed = await self.manager.start_campaign(
            self.user["id"],
            operation="greet",
            profile_id="window-failed",
            targets=["unknown.person"],
            message="窗口 B 的话术",
            messages=["窗口 B 的话术"],
            interval="1 秒",
            limit=1,
        )

        await self.manager.wait(success["id"])
        await self.manager.wait(failed["id"])

        success_stored = self.service.get_action_campaign(
            self.user["id"], success["id"]
        )
        failed_stored = self.service.get_action_campaign(
            self.user["id"], failed["id"]
        )
        self.assertEqual("completed", success_stored["status"])
        self.assertEqual("confirmed", success_stored["targets"][0]["status"])
        self.assertEqual("paused", failed_stored["status"])
        self.assertEqual("unknown", failed_stored["targets"][0]["status"])

        snapshot = self.service.get_workbench_snapshot(self.user["id"])
        self.assertEqual(1, snapshot["dedupe"]["greet_successes"])
        self.assertEqual(1, len(snapshot["action_success_history"]))
        history = snapshot["action_success_history"][0]
        self.assertEqual("window-success", history["profile_id"])
        self.assertEqual("success.person", history["targets"][0]["username"])
        self.assertEqual(success_message, history["attempts"][0]["message"])
        self.assertNotIn(
            "unknown.person",
            [
                item["targets"][0]["username"]
                for item in snapshot["action_success_history"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
