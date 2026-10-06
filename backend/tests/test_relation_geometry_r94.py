"""Large-gap regressions using production DOM scripts and explicit geometry.

This runs no browser. The Node adapter supplies independently computed layout
boxes; Python integration exercises the real durable reader and end checks.
"""
import asyncio
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.collection_surface import RELATION_ROWS_SCRIPT, relation_scroll_script, relation_position_status
from app.playwright_worker import PlaywrightWorker


def geometry(**options):
    process = subprocess.run(['node', str(Path(__file__).parent / 'support/relation_geometry.cjs')],
        input=json.dumps(dict(options=options, projection=RELATION_ROWS_SCRIPT,
            guarded_advance=relation_scroll_script('advance', []),
            **{action: relation_scroll_script(action) for action in ('advance', 'measure', 'reset')})),
        capture_output=True, text=True, timeout=15, check=True)
    return json.loads(process.stdout)


class RelationGeometryR94Tests(unittest.TestCase):
    def test_short_links_inside_taller_virtual_cards_never_become_invisible_required_anchors(self):
        # R20 real Chrome paints six 40px account cards, with a short inline link
        # near each card's top. Card centres and visible-link centres differ.
        for scale in (.75, 1, 1.5):
            with self.subTest(scale=scale):
                result = geometry(total=17, rowHeight=40, linkHeight=17, client=240,
                    clip=240, accountCards=True, virtualRows=6, positionGuard=True, scale=scale)
                self.assertEqual(17, result['seen'])
                self.assertTrue(result['frames'][-1]['measurement']['bottom'])
                self.assertEqual(3, sum(frame.get('movement', {}).get('moved') is True for frame in result['frames']))
                for before, after in zip(result['frames'], result['frames'][1:]):
                    movement = before['movement']
                    self.assertTrue(movement['anchor_rows'])
                    self.assertEqual('confirmed', relation_position_status(movement['anchor_rows'],
                        after['positioned_rows'], movement['scroll_shift']), (before, after))

    def test_thousand_short_link_virtual_cards_preserve_every_identity_and_anchor(self):
        for total, scale in ((1200, .75), (1237, 1.25)):
            with self.subTest(total=total, scale=scale):
                result = geometry(total=total, rowHeight=40, linkHeight=17, client=240,
                    clip=240, accountCards=True, virtualRows=6, positionGuard=True, scale=scale)
                self.assertEqual({f'/account_{n}/' for n in range(total)},
                    {href for frame in result['frames'] for href in frame['hrefs']})
                self.assertTrue(result['frames'][-1]['measurement']['bottom'])
                self.assertEqual(total * 40 - 240, result['frames'][-1]['inner'])
                for before, after in zip(result['frames'], result['frames'][1:]):
                    movement = before['movement']
                    self.assertTrue(movement['anchor_rows'])
                    self.assertEqual('confirmed', relation_position_status(movement['anchor_rows'],
                        after['positioned_rows'], movement['scroll_shift']))

    def test_insufficient_overlap_waits_in_place_without_unanchored_jump(self):
        result = geometry(total=20, rowHeight=40, client=240, clip=40, positionGuard=True)
        self.assertLess(len(result['frames']), 10)
        self.assertFalse(result['frames'][-1]['measurement']['bottom'])
        for current in result['frames'][:-1]:
            movement = current.get('movement', {})
            if movement.get('moved'):
                self.assertTrue(movement['anchor_rows'])
        final = result['frames'][-2]['movement']
        self.assertFalse(final['moved'])
        self.assertTrue(final['continuity_pending'])

    def test_single_visible_row_retains_an_anchor_in_narrow_or_tall_layouts(self):
        for row_height, clip in ((40, 60), (160, 240)):
            for scale in (.75, 1, 1.5):
                with self.subTest(row_height=row_height, clip=clip, scale=scale):
                    result = geometry(total=20, rowHeight=row_height, client=240,
                        clip=clip, scale=scale, positionGuard=True)
                    self.assertEqual(20, result['seen'])
                    self.assertTrue(result['frames'][-1]['measurement']['bottom'])
                    for before, after in zip(result['frames'], result['frames'][1:]):
                        movement = before['movement']
                        self.assertTrue(movement['anchor_rows'], 'a healthy move must retain at least one account')
                        self.assertEqual('confirmed', relation_position_status(movement['anchor_rows'],
                            after['positioned_rows'], movement['scroll_shift']))

    def test_screenshot_sized_lists_keep_every_account_with_position_guard(self):
        for total in (537, 369):
            for scale in (.75, 1.5):
                with self.subTest(total=total, scale=scale):
                    result = geometry(total=total, positionGuard=True, scale=scale, header=40, overflow='auto')
                    self.assertEqual(total, result['seen'])
                    self.assertTrue(result['frames'][-1]['measurement']['bottom'])
                    for before, after in zip(result['frames'], result['frames'][1:]):
                        movement = before['movement']
                        self.assertEqual('confirmed', relation_position_status(movement['anchor_rows'],
                            after['positioned_rows'], movement['scroll_shift']))

    def test_position_guard_covers_the_290_row_list_with_ordered_neighbours(self):
        for scale in (.75, 1, 1.5):
            result = geometry(total=290, positionGuard=True, scale=scale, header=40, overflow='auto')
            self.assertEqual(290, result['seen'])
            self.assertTrue(result['frames'][-1]['measurement']['bottom'])
            for before, after in zip(result['frames'], result['frames'][1:]):
                move = before['movement']
                self.assertTrue(move['anchor_rows'])
                self.assertEqual('confirmed', relation_position_status(move['anchor_rows'],
                    after['positioned_rows'], move['scroll_shift']))

    def test_unacknowledged_repaint_cannot_be_scrolled_past(self):
        first, after = geometry(total=30, positionGuard=True, acknowledgedOverride=['account_0'], single=True)['frames']
        self.assertTrue(first['movement']['continuity_pending'])
        self.assertFalse(first['movement']['moved'])
        self.assertEqual(first['inner'], after['inner'])

    def test_shared_username_alone_cannot_prove_order_or_position(self):
        anchors = [dict(username='a', expected_y=30), dict(username='b', expected_y=70)]
        self.assertEqual('confirmed', relation_position_status(anchors,
            [dict(username='a', y=30), dict(username='new', y=50), dict(username='b', y=70)], 140))
        for rows, expected in (([dict(username='b', y=30), dict(username='a', y=70)], 'anchor_order_changed'),
                ([dict(username='a', y=100), dict(username='b', y=140)], 'anchor_position_changed'),
                ([dict(username='b', y=70)], 'missing_anchor')):
            self.assertEqual(expected, relation_position_status(anchors, rows, 140))

    def test_offscreen_sibling_profiles_cannot_outvote_current_visible_list(self):
        first, after = geometry(total=1, rowHeight=1000, linkHeight=18,
            client=240, clip=260, header=40, outerContent=900,
            headerHrefs=['/source/', '/footer_a/', '/footer_b/', '/footer_c/'],
            headerLinkTops=[0, 800, 820, 840], overflow='auto', single=True)['frames']
        self.assertTrue(first['movement']['valid'])
        self.assertEqual(143, after['inner'])
        self.assertEqual(0, after['outer'])

    def test_preloaded_row_wrappers_do_not_cause_quadratic_container_search(self):
        for total in (200, 2000):
            with self.subTest(total=total):
                process = subprocess.run(['node', str(Path(__file__).parent /
                    'support/relation_scroll_selection.cjs')],
                    input=json.dumps({'total': total, 'measure': relation_scroll_script('measure')}),
                    capture_output=True, text=True, timeout=15, check=True)
                result = json.loads(process.stdout)
                self.assertTrue(result['measurement']['valid'])
                self.assertEqual(total * 40, result['measurement']['height'])
                self.assertFalse(result['measurement']['bottom'])
                self.assertLess(result['containsChecks'], total * 16,
                    'container selection must not compare every wrapper against every row')

    def test_offscreen_old_row_cannot_switch_measurement_to_header_shell(self):
        for header in (20, 40, 80):
            with self.subTest(header=header):
                first, after = geometry(total=1, rowHeight=1000, linkHeight=18,
                    client=240, clip=260, header=header, duplicateHeaderLinks=True,
                    overflow='auto', single=True)['frames']
                self.assertNotIn('/account_0/', after['hrefs'], 'offscreen row is not a candidate')
                self.assertTrue(after['measurement']['valid'])
                self.assertEqual(1000, after['measurement']['height'])
                self.assertEqual(after['inner'], after['measurement']['top'])
                self.assertFalse(after['measurement']['bottom'])
                self.assertEqual(0, after['outer'])

    def test_empty_repaint_frame_cannot_complete_and_restored_rows_recover_without_reset(self):
        first, gap, restored = geometry(total=20, client=240, clip=260, header=20,
            duplicateHeaderLinks=True, overflow='auto', single=True,
            detachRowsAfterAdvance=True, restoreRowsAfterGap=True)['frames']
        self.assertFalse(gap['measurement']['valid'])
        self.assertFalse(gap['measurement']['bottom'])
        self.assertEqual({'/source/'}, set(gap['hrefs']))
        self.assertTrue(restored['measurement']['valid'])
        self.assertFalse(restored['measurement']['bottom'])
        self.assertEqual(800, restored['measurement']['height'])
        self.assertEqual(156, restored['measurement']['top'])
        self.assertEqual(gap['inner'], restored['inner'])
        self.assertEqual(0, restored['outer'])
        self.assertTrue(set(restored['hrefs']) - {'/source/'})

    def test_external_and_reserved_header_links_cannot_outvote_real_account_viewport(self):
        for hrefs in ([f'https://example.test/outside{i}/' for i in range(4)],
                      ['/about/', '/legal/', '/privacy/', '/terms/']):
            with self.subTest(hrefs=hrefs):
                first, after = geometry(total=1, rowHeight=1000, linkHeight=18,
                    client=240, clip=260, header=40, headerHrefs=hrefs,
                    overflow='auto', single=True)['frames']
                self.assertTrue(first['movement']['valid'])
                self.assertEqual(143, after['inner'])
                self.assertEqual(0, after['outer'])

    def test_duplicate_header_fixture_uses_220_visible_pixels_not_240_client_pixels(self):
        # Same independent dimensions as SourceScrollerDOMR44Tests and the
        # reported Windows failure: 260px shell - 40px header = 220px visible.
        result = geometry(total=1, rowHeight=1000, linkHeight=18, client=240,
            clip=260, header=40, duplicateHeaderLinks=True, overflow='auto', single=True)
        first, second = result['frames']
        self.assertEqual(240, first['measurement']['client'])
        self.assertEqual(220, first['measurement']['visible_height'])
        self.assertTrue(first['movement']['valid'])
        self.assertTrue(first['movement']['moved'])
        self.assertFalse(first['movement']['bottom'])
        self.assertEqual(143, second['inner'])
        self.assertEqual(0, second['outer'])

    def test_header_and_scale_preserve_overlap_in_visible_layout_units(self):
        for header, visible, step in ((0, 240, 156), (40, 220, 143), (80, 180, 117)):
            for scale in (.75, 1, 1.25, 1.5):
                with self.subTest(header=header, scale=scale):
                    result = geometry(total=20, client=240, clip=260, header=header,
                        scale=scale, overflow='auto', single=True)
                    first, second = result['frames']
                    self.assertEqual(visible * scale, first['measurement']['visible_height'])
                    self.assertEqual(step, second['inner'])
                    self.assertEqual(0, second['outer'])
                    self.assertTrue(set(first['hrefs']) & set(second['hrefs']))

    def test_duplicate_header_and_mid_scan_resize_keep_every_account_without_rewind(self):
        for scale in (.75, 1, 1.5):
            with self.subTest(scale=scale):
                result = geometry(total=100, client=240, clip=260, header=40,
                    scale=scale, overflow='auto', duplicateHeaderLinks=True, resizeAt=3, resizeTo=160)
                observed = {href for frame in result['frames'] for href in frame['hrefs']}
                self.assertEqual({f'/account_{i}/' for i in range(100)}, observed - {'/source/'})
                self.assertTrue(result['frames'][-1]['measurement']['bottom'])
                for before, after in zip(result['frames'], result['frames'][1:]):
                    self.assertGreaterEqual(after['inner'], before['inner'])
                    self.assertGreaterEqual(after['outer'], before['outer'])

    def test_clipped_viewport_does_not_jump_over_unread_band(self):
        result = geometry(single=True)
        first, second = result['frames']
        self.assertLessEqual(second['inner'] - first['inner'], 260 * .65 + 1)
        self.assertGreater(second['inner'], first['inner'])
        self.assertTrue(set(first['hrefs']) & set(second['hrefs']))

    def test_inner_bottom_with_hidden_final_rows_is_not_list_end(self):
        first = geometry(startAtBottom=True, single=True)['frames'][0]
        self.assertFalse(first['measurement']['bottom'])

    def test_clipped_lists_keep_all_screenshot_sized_sources(self):
        for total in (469, 486, 205, 227, 166):
            with self.subTest(total=total):
                result = geometry(total=total)
                self.assertEqual(total, result['seen'])
                self.assertTrue(result['frames'][-1]['measurement']['bottom'])
                self.assertLess(len(result['frames']), total)
                self.assertTrue(all(frame['inner'] >= previous['inner'] and frame['outer'] >= previous['outer']
                    for previous, frame in zip(result['frames'], result['frames'][1:])), 'no rewind')

    def test_scrollable_outer_and_css_scale_keep_every_row(self):
        for overflow in ('auto', 'hidden'):
            for scale in (.75, 1.5):
                with self.subTest(overflow=overflow, scale=scale):
                    result = geometry(total=80, overflow=overflow, scale=scale)
                    self.assertEqual(80, result['seen'])
                    self.assertTrue(result['frames'][-1]['measurement']['bottom'])

    def test_resize_mid_scan_uses_new_visible_height(self):
        result = geometry(total=100, clip=500, resizeAt=3, resizeTo=220)
        self.assertEqual(100, result['seen'])
        self.assertTrue(result['frames'][-1]['measurement']['bottom'])

    def test_unscrollable_clip_never_confirms_hidden_tail(self):
        result = geometry(total=50, overflow='clip')
        self.assertFalse(result['frames'][-1]['measurement']['bottom'])

    def test_normal_unclipped_list_keeps_existing_overlap(self):
        result = geometry(total=60, clip=600, single=True)
        self.assertEqual(390, result['frames'][1]['inner'])

    def test_initial_reset_exposes_first_rows_inside_scrolled_parent(self):
        result = geometry(total=60, initialOuter=300, reset=True, single=True)
        self.assertIn('/account_0/', result['frames'][0]['hrefs'])

    def test_short_list_does_not_scroll_through_clipped_empty_padding(self):
        result = geometry(total=4)
        self.assertEqual(4, result['seen'])
        self.assertEqual(1, len(result['frames']))
        self.assertTrue(result['frames'][0]['measurement']['bottom'])

    def test_browser_edge_excludes_invisible_rows_and_cannot_prove_hidden_tail(self):
        result = geometry(total=50, clip=600, windowHeight=260)
        self.assertLessEqual(len(result['frames'][0]['hrefs']), 7)
        self.assertFalse(result['frames'][-1]['measurement']['bottom'])

    def test_failed_scroll_preserves_incomplete_state(self):
        result = geometry(total=50, stuck=True)
        self.assertFalse(result['frames'][0]['movement']['moved'])
        self.assertFalse(result['frames'][-1]['measurement']['bottom'])


class RelationGeometryReaderR94Tests(unittest.IsolatedAsyncioTestCase):
    async def test_jump_is_retried_in_place_and_cannot_reach_queue_or_completion(self):
        from app.playwright_worker import WorkerExecutionError
        frames = geometry(total=290, positionGuard=True)['frames']
        for repair_after in (None, 2):
            class Surface:
                index = 0
                reads = 0
                moves = 0
                async def evaluate(self, script):
                    if script == RELATION_ROWS_SCRIPT:
                        self.reads += 1
                        index = self.index
                        if self.index == 1 and (repair_after is None or self.reads <= repair_after + 2):
                            index = 5  # A virtualizer jumped over the retained neighbours.
                        return {k: frames[index][k] for k in ('hrefs', 'positioned_rows', 'recommendations_reached')}
                    if 'relation-action: reset' in script:
                        raise AssertionError('no automatic rewind')
                    if 'relation-action: advance' in script:
                        self.moves += 1
                        movement = frames[self.index].get('movement', {**frames[self.index]['measurement'], 'moved': False})
                        self.index = min(self.index + 1, len(frames) - 1)
                        return movement
                    return frames[self.index]['measurement']
            worker = PlaywrightWorker(None)
            worker._guard = AsyncMock()
            worker._relation_surface_failure = AsyncMock(return_value=None)
            worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
            worker.collection_loading_grace_seconds = 2
            worker.collection_poll_interval_seconds = 0
            worker.collection_settled_idle_rounds = 1
            clock = iter(i / 10 for i in range(100000))
            worker._collection_monotonic = lambda: next(clock)
            saved, submitted = set(), []
            async def sink(batch):
                saved.update(batch); submitted.extend(batch)
                return len(saved)
            surface = Surface()
            work = worker._read_visible_account_dialog(surface, None, candidate_sink=sink,
                surface_kind='followers', expected_minimum=290)
            if repair_after is None:
                with self.assertRaises(WorkerExecutionError) as caught:
                    await work
                self.assertEqual(1, surface.moves)
                self.assertEqual({row['username'] for row in frames[0]['positioned_rows']}, saved)
                self.assertEqual('missing_anchor', caught.exception.details['scroll_diagnostics']['continuity_status'])
            else:
                await work
                self.assertEqual({f'account_{i}' for i in range(290)}, saved)
                self.assertEqual(290, len(submitted))

    async def test_real_completion_guard_rejects_header_only_after_scroll(self):
        after = geometry(total=1, rowHeight=1000, linkHeight=18, client=240,
            clip=260, header=20, duplicateHeaderLinks=True, overflow='auto', single=True)['frames'][1]
        worker = PlaywrightWorker(None)
        worker._guard = AsyncMock()
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        worker._relation_surface_failure = AsyncMock(return_value=None)
        surface = SimpleNamespace(evaluate=AsyncMock(return_value=after['measurement']))
        self.assertFalse(await worker._confirm_relation_list_end(surface))
        self.assertIsNone(worker._relation_end_measurement)

    async def test_resize_between_bottom_measurements_invalidates_end(self):
        worker = PlaywrightWorker(None)
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        full = dict(valid=True, bottom=True, top=600, height=1000, client=400,
            visible_height=400, viewport_top=0, viewport_bottom=400)
        surface = SimpleNamespace(evaluate=AsyncMock(side_effect=[full, {**full, 'visible_height': 250}]))
        self.assertFalse(await worker._confirm_relation_list_end(surface))

    async def test_resize_after_last_frame_invalidates_end(self):
        worker = PlaywrightWorker(None)
        worker._relation_end_measurement = dict(valid=True, bottom=True, top=600, height=1000, client=400,
            visible_height=400, viewport_top=0, viewport_bottom=400)
        surface = SimpleNamespace(evaluate=AsyncMock(return_value={**worker._relation_end_measurement,
            'viewport_bottom': 250}))
        self.assertFalse(await worker._relation_list_end_is_current(surface))

    async def test_durable_reader_saves_every_account_before_end_without_reset(self):
        for relation, total, hover in (('followers', 469, False), ('following', 486, True)):
            with self.subTest(relation=relation, hover=hover):
                replay = geometry(total=total)
                frames = replay['frames']
                class Surface:
                    index = 0
                    async def evaluate(self, script):
                        frame = frames[self.index]
                        if script == RELATION_ROWS_SCRIPT:
                            return {'hrefs': frame['hrefs'], 'recommendations_reached': False}
                        if 'relation-action: reset' in script:
                            raise AssertionError('no automatic rewind')
                        if 'relation-action: advance' in script:
                            self.index = min(self.index + 1, len(frames) - 1)
                            return frame.get('movement', {**frame['measurement'], 'moved': False})
                        return frame['measurement']
                worker = PlaywrightWorker(None)
                worker._guard = AsyncMock()
                worker._relation_surface_failure = AsyncMock(return_value=None)
                worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
                worker._read_relation_hover_preview = AsyncMock(return_value={'posts': 10})
                worker.collection_loading_grace_seconds = 0
                worker.collection_poll_interval_seconds = 0
                worker.collection_settled_idle_rounds = 1
                saved, submitted = set(), []
                async def sink(batch, previews=None):
                    if hover:
                        self.assertEqual(set(batch), set(previews))
                    saved.update(batch); submitted.extend(batch)
                    return len(saved)
                await asyncio.wait_for(worker._read_visible_account_dialog(Surface(), None,
                    surface_kind=relation, candidate_sink=sink, expected_minimum=total,
                    hover_precheck=hover), 10)
                self.assertEqual({f'account_{i}' for i in range(total)}, saved)
                self.assertEqual(total, len(submitted), 'no repeated writes across overlapping frames')


if __name__ == '__main__':
    unittest.main()
