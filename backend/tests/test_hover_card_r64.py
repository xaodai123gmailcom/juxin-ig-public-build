"""Hover cards rendered in a portal and cleanup of r61-r63 false exclusions."""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database import Database
from app import __source_revision__
from app.execution_manager import ExecutionControl, ExecutionManager
from app.profile_hover_preview import HOVER_PREVIEW_SCRIPT, hover_count_exclusion
from app.playwright_worker import PlaywrightWorker
from app.playwright_worker import WorkerExecutionError
from app.service import CoreService


class HoverCardDomTests(unittest.TestCase):
    def test_plain_text_username_in_independent_hover_portal(self):
        harness = r"""
          class Element {
            constructor(text, width, height, children = []) {
              this.innerText = text; this.children = children;
              this.width = width; this.height = height;
            }
            getBoundingClientRect() { return {width: this.width, height: this.height}; }
            querySelectorAll(selector) {
              if (selector === 'a[href]') return [];
              return this.children;
            }
          }
          const metrics = ['24\n帖子', '32\n粉丝', '240\n已关注']
            .map(text => new Element(text, 95, 45));
          const portal = new Element('sample_hover01\n24 帖子\n32 粉丝\n240 已关注',
            366, 336, metrics);
          const unrelated = new Element('other_account\n5000 帖子\n5000 粉丝\n5000 已关注',
            360, 330, []);
          globalThis.getComputedStyle = () => ({display:'block', visibility:'visible'});
          globalThis.location = {href:'https://www.instagram.com/source/'};
          globalThis.document = {querySelectorAll: () => [unrelated, portal]};
          const read = eval('(' + SCRIPT + ')');
          const found = read('sample_hover01');
          if (!found || found.posts !== 24 || found.followers !== 32 ||
              found.following !== 240 || found.evidence !== 'relationship_hover_card')
            throw new Error(JSON.stringify(found));
          globalThis.document = {querySelectorAll: () => [unrelated]};
          if (read('sample_hover01') !== null)
            throw new Error('another account must not satisfy the hovered username');
          const divided = new Element('sample_hover_split01\n16\n帖子\n640\n粉丝\n160\n已关注',
            366, 336, ['16', '帖子', '640', '粉丝', '160', '已关注']
              .map(text => new Element(text, 80, 25)));
          globalThis.document = {querySelectorAll: () => [divided]};
          const split = read('sample_hover_split01');
          if (!split || split.posts !== 16 || split.followers !== 640 ||
              split.following !== 160) throw new Error(JSON.stringify(split));
          const zeroStats = ['0\n帖子', '34\n粉丝', '360\n已关注']
            .map(text => new Element(text, 85, 30));
          const zero = new Element('sample_hover_empty01\n0 帖子\n34 粉丝\n360 已关注\n还没有帖子',
            382, 350, zeroStats);
          globalThis.document = {querySelectorAll: () => [zero]};
          const empty = read('sample_hover_empty01');
          if (!empty || empty.posts !== 0 || empty.visibility !== 'public')
            throw new Error(JSON.stringify(empty));
          const unproven = new Element('another.zero\n0 帖子\n34 粉丝\n360 已关注',
            382, 350, zeroStats);
          globalThis.document = {querySelectorAll: () => [unproven]};
          if (read('another.zero')?.visibility !== 'unknown')
            throw new Error('zero count alone must not prove public visibility');
          const privateEmpty = new Element('private.zero\n0 帖子\n34 粉丝\n360 已关注\n还没有帖子\n私人帳號',
            382, 350, zeroStats);
          globalThis.document = {querySelectorAll: () => [privateEmpty]};
          if (read('private.zero')?.visibility !== 'private')
            throw new Error('a private label takes precedence over zero-post text');
          const syntheticPrivate = new Element('sample_hover_private01\n7 帖子\n92 粉丝\n540 已关注\n这是私密账户',
            382, 350, ['7 帖子', '92 粉丝', '540 已关注']
              .map(text => new Element(text, 85, 30)));
          globalThis.document = {querySelectorAll: () => [syntheticPrivate]};
          if (read('sample_hover_private01')?.visibility !== 'private')
            throw new Error('the exact localized private label must be recognized');
          const abbreviated = new Element('rounded.user\n7 帖子\n4.5K 粉丝\n540 已关注',
            382, 350, ['7 帖子', '4.5K 粉丝', '540 已关注']
              .map(text => new Element(text, 85, 30)));
          globalThis.document = {querySelectorAll: () => [abbreviated]};
          const rounded = read('rounded.user');
          if (!rounded || rounded.posts !== 7 || rounded.followers !== 4400 ||
              rounded.following !== 540 || rounded.bounded_counts?.followers?.display !== '4.5K')
            throw new Error('rounded follower lower bound and exact following must remain separate: ' + JSON.stringify(rounded));
          const mixedChildren = new Element('rounded.mixed\n7 帖子\n4.5K 粉丝\n540 已关注',
            382, 350, [new Element('7 帖子', 85, 30),
              new Element('4.5K 粉丝 540', 130, 30),
              new Element('已关注', 85, 30)]);
          globalThis.document = {querySelectorAll: () => [mixedChildren]};
          const mixed = read('rounded.mixed');
          if (!mixed || mixed.followers !== 4400 || mixed.following !== 540 ||
              mixed.bounded_counts?.followers?.minimum !== 4400)
            throw new Error('a complete triplet must preserve the real adjacent metric: ' + JSON.stringify(mixed));
          globalThis.document = {querySelectorAll: () => [abbreviated]};
          abbreviated.innerText = 'rounded.user\n7 帖子\n1.2万 粉丝\n540 已关注';
          abbreviated.children[1].innerText = '1.2万 粉丝';
          const chinese = read('rounded.user');
          if (!chinese || chinese.followers !== 11000 ||
              chinese.bounded_counts?.followers?.display !== '1.2万')
            throw new Error('a Chinese abbreviated count must record its conservative floor: ' + JSON.stringify(chinese));
          const syntheticCard = new Element('sample_hover_large01\n1200 帖子\n6.4万 粉丝\n1500 已关注',
            366, 336, ['1200 帖子', '6.4万 粉丝', '1500 已关注']
              .map(text => new Element(text, 90, 30)));
          globalThis.document = {querySelectorAll: () => [syntheticCard]};
          const high = read('sample_hover_large01');
          if (!high || high.posts !== 1200 || high.followers !== 63000 ||
              high.following !== 1500 || high.bounded_counts?.followers?.minimum !== 63000)
            throw new Error('synthetic hover must prove a high follower lower bound: ' + JSON.stringify(high));
          syntheticCard.innerText = 'sample_hover_large01\n1200 帖子\n4.1K 粉丝\n1500 已关注';
          syntheticCard.children[1].innerText = '4.1K 粉丝';
          if (read('sample_hover_large01')?.followers !== 4000)
            throw new Error('near-threshold abbreviations must preserve uncertainty');
          const localized = [
            ['ja', ['投稿 178', 'フォロワー 1.2万', 'フォロー中 892'], 11000],
            ['ko', ['게시물 178', '팔로워 1.2만', '팔로잉 892'], 11000],
            ['th', ['178 โพสต์', '12.3K ผู้ติดตาม', '892 กำลังติดตาม'], 12200],
            ['es', ['178 publicaciones', '12,3 mil seguidores', '892 siguiendo'], 12200],
            ['pt', ['178 publicações', '12,3 mil seguidores', '892 seguindo'], 12200],
            ['id', ['178 postingan', '12,3 rb pengikut', '892 mengikuti'], 12200],
          ];
          for (const [language, fields, lowerBound] of localized) {
            const localizedCard = new Element(language + '.user\n' + fields.join('\n'),
              366, 336, fields.map(text => new Element(text, 115, 30)));
            globalThis.document = {querySelectorAll: () => [localizedCard]};
            const found = read(language + '.user');
            if (!found || found.posts !== 178 || found.followers !== lowerBound ||
                found.following !== 892 || found.bounded_counts?.followers?.minimum !== lowerBound)
              throw new Error('localized hover did not preserve metric labels: ' + language + ' ' + JSON.stringify(found));
            if (language === 'ja' || language === 'es') {
              localizedCard.children = [];
              const wholeText = read(language + '.user');
              if (!wholeText || wholeText.posts !== 178 || wholeText.followers !== lowerBound ||
                  wholeText.following !== 892)
                throw new Error('localized split-node fallback failed: ' + language + ' ' + JSON.stringify(wholeText));
            }
          }
          const incomplete = new Element('unknown\n1200 帖子\n6.4万 粉丝\n关注未知',
            366, 336, ['1200 帖子', '6.4万 粉丝', '关注未知']
              .map(text => new Element(text, 90, 30)));
          globalThis.document = {querySelectorAll: () => [incomplete]};
          if (read('unknown') !== null)
            throw new Error('an unreadable third metric cannot certify a hover card');
          const labelFirst = new Element('label.first\n帖子 7\n粉丝 34\n已关注 360',
            382, 350, ['帖子 7', '粉丝 34', '已关注 360']
              .map(text => new Element(text, 85, 30)));
          globalThis.document = {querySelectorAll: () => [labelFirst]};
          if (read('label.first')?.followers !== 34)
            throw new Error('an isolated label-first metric remains readable');
          const crossedFollowing = new Element('crossed.following\n帖子 7 粉丝 5000 已关注 20',
            382, 350, ['帖子 7', '粉丝', '5000 已关注', '20']
              .map(text => new Element(text, 85, 30)));
          globalThis.document = {querySelectorAll: () => [crossedFollowing]};
          if (read('crossed.following') !== null)
            throw new Error('a preceding follower count must not become following 5000');
          const crossedBoth = new Element('crossed.both\n帖子 20 粉丝 5000 已关注 7',
            382, 350, ['帖子', '20 粉丝', '5000 已关注', '7']
              .map(text => new Element(text, 85, 30)));
          globalThis.document = {querySelectorAll: () => [crossedBoth]};
          if (read('crossed.both') !== null)
            throw new Error('adjacent label-first counts cannot certify the wrong metric');
          const missingGroup = new Element('missing.group\n帖子 7\n粉丝 5000\n简介\n已关注 20',
            382, 350, ['帖子 7', '粉丝 5000', '已关注 20']
              .map(text => new Element(text, 85, 30)));
          globalThis.document = {querySelectorAll: () => [missingGroup]};
          if (read('missing.group') !== null)
            throw new Error('three unrelated fragments cannot replace a complete stats group');
        """
        script = 'const SCRIPT = ' + json.dumps(HOVER_PREVIEW_SCRIPT) + ';\n' + harness
        result = subprocess.run(['node', '-e', script], encoding="utf-8",
                                capture_output=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_rounded_floor_only_excludes_above_both_saved_limits(self):
        preview = {
            'username': 'sample_hover_large01', 'posts': 1200,
            'followers': 63000, 'following': 1500, 'visibility': 'unknown',
            'evidence': 'relationship_hover_card',
            'bounded_counts': {'followers': {'display': '6.4万', 'minimum': 63000}},
        }
        normal = {'public_discard_followers_max': 4000,
                  'private_discard_followers_max': 4000}
        reason, exceeded = hover_count_exclusion(preview, normal)
        self.assertEqual('account_count_ceiling_exceeded', reason)
        self.assertEqual(63000, exceeded['followers']['actual'])
        # Unknown privacy may take either route; crossing only one saved rule
        # is insufficient to discard the identity before full page review.
        self.assertIsNone(hover_count_exclusion(preview, {
            **normal, 'private_discard_followers_max': 70000}))
        self.assertIsNone(hover_count_exclusion(preview, {
            **normal, 'discard_count_limits_enabled': False}))
        self.assertIsNone(hover_count_exclusion({**preview, 'followers': 4000,
            'bounded_counts': {'followers': {'display': '4.1K', 'minimum': 4000}}}, normal))
        self.assertIsNone(hover_count_exclusion({**preview,
            'bounded_counts': {'followers': {'display': '6.4万', 'minimum': 99999}}}, normal))
        self.assertIsNone(hover_count_exclusion({**preview,
            'posts': 0, 'visibility': 'public',
            'bounded_counts': {'posts': {'display': '0K', 'minimum': 0}}},
            {'exclude_public_zero_posts': True, 'discard_count_limits_enabled': False}))


class HoverHistoryRepairTests(unittest.TestCase):
    def test_core_revision_matches_build_metadata(self):
        root = Path(__file__).resolve().parents[2]
        revision = (root / 'BUILD_REVISION.txt').read_text().split('Source revision: ', 1)[1].splitlines()[0]
        self.assertEqual(revision, __source_revision__)

    def test_only_unverified_hover_history_is_retracted_and_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'collection.sqlite3')
            db.initialize()
            service = CoreService(db)
            owner = service.register_user('hover-repair-owner', 'hover-repair-password')['id']
            manager = ExecutionManager(service, SimpleNamespace())
            task = service.create_task(owner, name='old hover', modes=['followers'],
                targets=['source.one'], settings={'local_person_recognition': False})
            target = task['targets'][0]['id']
            event = asyncio.Event(); event.set()
            control = ExecutionControl(owner_user_id=owner, task_id=task['id'],
                pause_event=event, stop_event=asyncio.Event(), leases={},
                target_queue=asyncio.Queue())

            def exclude(username, reason):
                claim = service.claim_workbench_identity(owner, username=username,
                    source='followers', source_target=target, allow_owned_resume=True)
                profile = {'username': username,
                           'page_read_status': 'hover_preview_unavailable' if reason == 'hover_preview_unavailable' else 'hover_preview',
                           'page_read_reason': 'hover_card_missing_or_incomplete' if reason == 'hover_preview_unavailable' else 'exact_hover_card_counts'}
                screening = {'review_reason': reason}
                manager._record_collection_exclusion(control, target_id=target,
                    username=username, instagram_user_id=None, mode='followers',
                    profile=profile, screening=screening, claim_id=claim['claim_id'],
                    reason_code=reason, reason='历史排除')

            exclude('wrongly.excluded', 'hover_preview_unavailable')
            exclude('actually.excluded', 'account_count_ceiling_exceeded')
            self.assertTrue(service.check_global_dedupe('wrongly.excluded')['seen'])
            with db.write() as connection:
                connection.execute('DELETE FROM schema_migrations WHERE version=37')
            db.initialize()
            self.assertFalse(service.check_global_dedupe('wrongly.excluded')['seen'])
            exclude('newly.excluded', 'hover_preview_unavailable')
            self.assertTrue(service.check_global_dedupe('newly.excluded')['seen'])
            db.initialize()
            self.assertFalse(service.check_global_dedupe('newly.excluded')['seen'])
            self.assertTrue(Path(str(db.path) + '.pre-hover-r65.bak').exists())
            self.assertTrue(service.check_global_dedupe('actually.excluded')['seen'])
            with db.read() as connection:
                self.assertEqual(2, connection.execute(
                    'SELECT COUNT(*) FROM hover_r64_repair_archive').fetchone()[0])
            self.assertTrue(Path(str(db.path) + '.pre-hover-r64.bak').exists())
            db.initialize()
            self.assertFalse(service.check_global_dedupe('wrongly.excluded')['seen'])


class HoverRowNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_virtualized_nth_rebind_cannot_hover_another_account(self):
        expected = {'username': 'target', 'posts': 12, 'followers': 34,
                    'following': 40, 'evidence': 'relationship_hover_card'}
        recycled = SimpleNamespace(get_attribute=AsyncMock(return_value='/other/'),
                                   hover=AsyncMock())
        target = SimpleNamespace(get_attribute=AsyncMock(return_value='/target/'),
                                 hover=AsyncMock())
        rows = [SimpleNamespace(element_handle=AsyncMock(return_value=recycled)),
                SimpleNamespace(element_handle=AsyncMock(return_value=target))]
        links = SimpleNamespace(
            evaluate_all=AsyncMock(return_value=['/target/', '/target/']),
            nth=lambda index: rows[index])
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected))
        worker._collection_checkpoint = AsyncMock()
        self.assertEqual(expected, await worker._read_relation_hover_preview(
            SimpleNamespace(locator=lambda _: links), 'target'))
        recycled.hover.assert_not_awaited()
        target.hover.assert_awaited_once()

    async def test_existing_same_name_card_must_close_before_hover(self):
        expected = {'username': 'target', 'posts': 12, 'followers': 34,
                    'following': 40, 'evidence': 'relationship_hover_card'}
        current = {'card': expected, 'moves': 0}

        async def move(_x, _y):
            current['moves'] += 1
            current['card'] = None

        async def hover(**_kwargs):
            current['card'] = expected

        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/target/'),
                              hover=AsyncMock(side_effect=hover))
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=lambda *_: current['card']),
            mouse=SimpleNamespace(move=AsyncMock(side_effect=move)))
        worker._collection_checkpoint = AsyncMock()
        self.assertEqual(expected, await worker._read_relation_hover_preview(
            SimpleNamespace(locator=lambda _: SimpleNamespace(
                count=AsyncMock(return_value=1), nth=lambda _: row)), 'target'))
        self.assertEqual(1, current['moves'])
        row.hover.assert_awaited_once()
        self.assertEqual(1, worker.last_relation_hover_diagnostics['stale_cards'])

    async def test_recycled_row_after_hover_cannot_confirm_card(self):
        expected = {'username': 'target', 'posts': 12, 'followers': 34,
                    'following': 40, 'evidence': 'relationship_hover_card'}
        row = SimpleNamespace(get_attribute=AsyncMock(side_effect=[
            '/target/', '/target/', '/other/', '/other/']), hover=AsyncMock())
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected))
        worker._collection_checkpoint = AsyncMock()
        self.assertIsNone(await worker._read_relation_hover_preview(
            SimpleNamespace(locator=lambda _: SimpleNamespace(
                count=AsyncMock(return_value=1), nth=lambda _: row)), 'target'))
        row.hover.assert_awaited_once()
        self.assertGreater(worker.last_relation_hover_diagnostics['row_recycled'], 0)

    async def test_short_username_is_found_after_more_than_twelve_prefix_matches(self):
        expected = {'username': 'a', 'posts': 7, 'followers': 34,
                    'following': 360, 'evidence': 'relationship_hover_card'}
        rows = [SimpleNamespace(get_attribute=AsyncMock(return_value=f'/a{i}/'),
                                hover=AsyncMock()) for i in range(12)]
        real = SimpleNamespace(get_attribute=AsyncMock(return_value='/a/'), hover=AsyncMock())
        rows.append(real)
        links = SimpleNamespace(count=AsyncMock(return_value=len(rows)), nth=lambda i: rows[i])
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected))
        worker._collection_checkpoint = AsyncMock()
        self.assertEqual(expected, await worker._read_relation_hover_preview(
            SimpleNamespace(locator=lambda _: links), 'a'))
        real.hover.assert_awaited_once()
        self.assertTrue(all(row.hover.await_count == 0 for row in rows[:-1]))

    async def test_missing_row_is_rechecked_before_pausing(self):
        expected = {'username': 'recycled', 'posts': 7, 'followers': 34,
                    'following': 360, 'evidence': 'relationship_hover_card'}
        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/recycled/'),
                              hover=AsyncMock())
        calls = iter((SimpleNamespace(count=AsyncMock(return_value=0), nth=lambda _: None),
                      SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: row)))
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected))
        worker._collection_checkpoint = AsyncMock()
        self.assertEqual(expected, await worker._read_relation_hover_preview(
            SimpleNamespace(locator=lambda _: next(calls)), 'recycled'))
        row.hover.assert_awaited_once()

    async def test_closed_source_page_is_reported_as_connection_failure(self):
        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/closed.source/'),
                              hover=AsyncMock(side_effect=RuntimeError(
                                  'Target page, context or browser has been closed')))
        links = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: row)
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=None))
        worker._collection_checkpoint = AsyncMock()
        with self.assertRaises(WorkerExecutionError) as error:
            await worker._read_relation_hover_preview(
                SimpleNamespace(locator=lambda _: links), 'closed.source')
        self.assertEqual('worker_not_connected', error.exception.code)

    async def test_hover_failure_persists_previously_verified_frame_prefix(self):
        worker = PlaywrightWorker(object())
        worker._read_visible_account_hrefs = AsyncMock(return_value=['/first/', '/second/'])
        worker._read_relation_hover_preview = AsyncMock(side_effect=[
            {'username': 'first', 'posts': 7, 'followers': 34, 'following': 360,
             'visibility': 'unknown', 'evidence': 'relationship_hover_card'}, None])
        worker._collection_checkpoint = AsyncMock()
        committed = []
        async def sink(names, previews):
            committed.append((list(names), dict(previews)))
            return len(names)
        with self.assertRaises(WorkerExecutionError) as error:
            await worker._read_visible_account_dialog(
                SimpleNamespace(), None, surface_kind='followers',
                candidate_sink=sink, hover_precheck=True)
        self.assertEqual('instagram_hover_card_unavailable', error.exception.code)
        self.assertEqual([['first']], [names for names, _ in committed])
        self.assertEqual('first', committed[0][1]['first']['username'])

    async def test_hover_repaint_is_read_before_next_scroll(self):
        worker = PlaywrightWorker(object())
        worker._read_visible_account_hrefs = AsyncMock(side_effect=[
            ['/first/'], ['/first/', '/late/']])
        worker._read_relation_hover_preview = AsyncMock(side_effect=lambda _dialog, name: {
            'username': name, 'posts': 2, 'followers': 3, 'following': 4,
            'evidence': 'relationship_hover_card',
        })
        worker._collection_checkpoint = AsyncMock()
        class SawLateRow(Exception):
            pass
        committed = []
        async def sink(names, _previews):
            if 'late' in names:
                raise SawLateRow
            committed.extend(names)
            return len(committed)
        dialog = SimpleNamespace(evaluate=AsyncMock())
        with self.assertRaises(SawLateRow):
            await worker._read_visible_account_dialog(
                dialog, None, surface_kind='followers',
                candidate_sink=sink, hover_precheck=True)
        self.assertEqual(['first'], committed)
        self.assertEqual(['first', 'late'], [call.args[1]
            for call in worker._read_relation_hover_preview.await_args_list])
        dialog.evaluate.assert_not_awaited()

    async def test_visible_row_with_query_string_is_hovered_before_card_read(self):
        expected = {'username': 'sample_hover01', 'posts': 24, 'followers': 32,
                    'following': 240, 'evidence': 'relationship_hover_card'}
        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/sample_hover01/?igsh=abc'),
                              hover=AsyncMock())
        links = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: row)
        dialog = SimpleNamespace(locator=lambda selector: links)
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected))
        worker._collection_checkpoint = AsyncMock()
        self.assertEqual(expected,
            await worker._read_relation_hover_preview(dialog, 'sample_hover01'))
        row.hover.assert_awaited_once()
        self.assertEqual('sample_hover01', worker.page.evaluate.await_args.args[1])

    async def test_rebinds_row_if_list_recycles_it_during_hover(self):
        expected = {'username': 'sample_hover_split01', 'posts': 16, 'followers': 640,
                    'following': 160, 'evidence': 'relationship_hover_card'}
        first = SimpleNamespace(get_attribute=AsyncMock(return_value='/sample_hover_split01/'),
                                hover=AsyncMock(side_effect=RuntimeError('detached')))
        second = SimpleNamespace(get_attribute=AsyncMock(return_value='/sample_hover_split01/'),
                                 hover=AsyncMock())
        visits = iter((first, second))
        dialog = SimpleNamespace(locator=lambda selector: SimpleNamespace(
            count=AsyncMock(return_value=1), nth=lambda _: next(visits)))
        worker = PlaywrightWorker(object())
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected))
        worker._collection_checkpoint = AsyncMock()
        self.assertEqual(expected, await worker._read_relation_hover_preview(dialog, 'sample_hover_split01'))
        first.hover.assert_awaited_once()
        second.hover.assert_awaited_once()

    async def test_background_source_uses_its_own_cdp_hover_when_card_is_delayed(self):
        expected = {'username': 'sample_hover_split01', 'posts': 16, 'followers': 640,
                    'following': 160, 'evidence': 'relationship_hover_card'}
        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/sample_hover_split01/'),
                              hover=AsyncMock(), bounding_box=AsyncMock(
                                  return_value={'x': 60, 'y': 100, 'width': 80, 'height': 24}))
        dialog = SimpleNamespace(locator=lambda _: SimpleNamespace(
            count=AsyncMock(return_value=1), nth=lambda _: row))
        worker = PlaywrightWorker(object())
        # One baseline read precedes the first hover; the popup becomes
        # available only after the direct CDP fallback.
        worker.page = SimpleNamespace(evaluate=AsyncMock(side_effect=[None] * 17 + [expected]))
        worker._cdp_session = SimpleNamespace(send=AsyncMock(return_value={}))
        worker._collection_checkpoint = AsyncMock()
        worker._ensure_window_surface_stable = AsyncMock()
        async def run_lifecycle(coro, **kwargs):
            return await coro
        worker._await_lifecycle_operation = run_lifecycle
        self.assertEqual(expected, await worker._read_relation_hover_preview(dialog, 'sample_hover_split01'))
        self.assertEqual([
            ('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 1, 'y': 1}),
            ('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 100, 'y': 112}),
        ], [call.args for call in worker._cdp_session.send.await_args_list])
        row.hover.assert_awaited_once()
