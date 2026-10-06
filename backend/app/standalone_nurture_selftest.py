"""Offline proof executed by the exact installed Core, with no real account.

The production manager, service, SQLite, StudioBrowser and StandaloneNurture are
exercised together. Only the browser/DOM responses, clock and random draws are
synthetic. All state belongs to a fresh temporary directory; network access is
rejected and no user settings, credentials or browser profiles are loaded.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager, closing, contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .database import Database
from .errors import ConflictError, ValidationError
from .service import CoreService
from .studio import StudioManager, config_for, build_nurture_steps
from . import studio, standalone_nurture

PROOF_PREFIX = 'STANDALONE_NURTURE_SELFTEST=PASS '
POLICY = 'standalone-reels-8-20-70-v1'


def require(value, message):
    if not value:
        raise RuntimeError('Standalone nurture installed self-test: ' + message)


def records(database, table):
    with database.read() as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]


class Clock:
    """Task-local active time also inherited by the owned cleanup task."""
    def __init__(self):
        self.value = ContextVar('standalone_offline_clock', default=1000.)
    def now(self):
        return self.value.get()
    def advance(self, seconds):
        self.value.set(self.now() + seconds)
    async def sleep(self, seconds):
        # Sub-ULP residuals in resumed budgets still advance the fake clock.
        self.advance(max(float(seconds), .000001))
        await asyncio.sleep(0)


class Locator:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind
    @property
    def first(self):
        return self
    def nth(self, index):
        require(index == 0, 'synthetic DOM selected a nonunique control')
        return self
    def locator(self, selector):
        return Locator(self.page, 'heart')
    def filter(self, **kwargs):
        return self
    def and_(self, other):
        return self
    def or_(self, other):
        return self
    async def count(self):
        if self.kind == 'next' and self.page.mode == 'keyboard_next':return 0
        return 0 if self.kind == 'restriction' else 1
    async def is_visible(self):
        return True
    async def click(self, **kwargs):
        page = self.page
        page.fixture.assert_owned(page.profile)
        require(page.snapshot_seen(), 'effect preceded durable verified own-profile snapshot')
        if self.kind == 'next':
            if page.mode == 'pause_after_confirmed':
                raise asyncio.CancelledError()
            if page.mode == 'advance_noop':
                page.events.append('next-noop')
                return
            page.index += 1
            page.update_reel_route()
            page.events.append('next')
            return
        require(self.kind == 'heart', 'unexpected synthetic effect')
        key = page.current()['key']
        job = page.fixture.job(page.profile)
        result = json.loads(job['result_json'])
        require(result['nurture_decisions'][key]['selected'] is True, 'click preceded durable 70 percent decision')
        require(job['inflight'] == 1 and result['nurture_actions']['like:' + key]['state'] == 'pending',
                'click preceded durable pending boundary')
        require(key not in page.clicked, 'same video was clicked twice')
        page.clicked.append(key)
        page.events.append('like:' + key)
        if page.mode == 'interrupted':
            raise asyncio.CancelledError()
        page.states[key] = 'liked'
    async def press(self, key, **kwargs):
        require(self.kind == 'video' and key == 'ArrowDown', 'unexpected synthetic key')
        self.page.fixture.assert_owned(self.page.profile)
        self.page.index += 1
        self.page.events.append('key:ArrowDown')


class Page:
    sequence = ('A', 'B', 'A', 'C', 'D', 'E', 'F', 'G')
    def __init__(self, fixture, profile, mode='normal'):
        self.fixture, self.profile, self.mode = fixture, profile, mode
        self.url = 'https://www.instagram.com/'
        self.context = self
        self.username = 'offline_' + profile.replace('-', '_')
        self.index = 0
        self.states = {'/reel/C': 'unknown', '/reel/D': 'liked'}
        self.clicked, self.events = [], []
        self.draws = 0
        self.route_forms = set()
        self.media_started = None
        self.disconnected = False
    async def cookies(self, url):
        uid = '999999' if self.mode == 'identity_changed' and self.url.endswith('/' + self.username + '/') else '123456'
        return [{'name': 'ds_user_id', 'value': uid}]
    def snapshot_seen(self):
        data = json.loads(self.fixture.job(self.profile)['result_json']).get('account_snapshot', {})
        return data.get('username') == self.username and data.get('instagram_user_id') == '123456'
    async def goto(self, url, **kwargs):
        self.fixture.assert_owned(self.profile)
        if url == standalone_nurture.REELS_URL:
            require(self.snapshot_seen(), 'Reels navigation preceded durable verified own-profile snapshot')
        self.url = url
        if url == standalone_nurture.REELS_URL:
            self.media_started = self.fixture.clock.now()
            self.update_reel_route()
        self.events.append('goto:' + url)
        self.fixture.clock.advance(.4)
    def update_reel_route(self):
        if self.mode == 'plural_routes':
            route = 'reels' if self.index % 2 == 0 else 'reel'
            self.url = 'https://www.instagram.com/' + route + self.current()['key'][5:] + '/?fixture=offline'
    def current(self):
        suffix = self.sequence[self.index] if self.index < len(self.sequence) else 'Later' + str(self.index)
        key = '/reel/' + suffix
        media_time=self.fixture.clock.now()
        if self.mode=='frozen_playback':media_time=0.0
        if self.mode=='buffering':media_time=max(0.0,media_time-(self.media_started or media_time)-2.0)
        return {'key': key, 'state': self.states.get(key, 'unliked'), 'source': 'synthetic:' + key,
                'time': media_time}
    async def evaluate(self, expression, *args):
        if expression == standalone_nurture.OWN_LINK:
            return '' if self.mode == 'unknown_owner' else self.username
        if expression == standalone_nurture.OWN_METRICS:
            require(args == (self.username,), 'own-profile reader used another account')
            self.events.append('own-profile')
            return {'posts_count': '0', 'followers_count': '1,234',
                    'following_count': '1.2K' if self.mode == 'partial_counts' else '28' if self.mode == 'resumed_counts' else '27'}
        if expression == standalone_nurture.REEL:
            require(self.snapshot_seen(), 'Reels read preceded own-profile verification')
            if self.mode == 'plural_routes':
                self.route_forms.add('plural' if '/reels/' in self.url else 'singular')
            return self.current()
        if expression == standalone_nurture.ADVANCE_GUARD:
            return True
        # capture_executor is allowed to read the same verified synthetic identity.
        from .instagram_home import OWN_PROFILE
        if expression == OWN_PROFILE:
            return self.username
        raise RuntimeError('Unexpected DOM expression in offline self-test')
    def locator(self, selector):
        if selector.startswith('video[data-standalone-video='):return Locator(self,'video')
        return Locator(self, 'heart')
    def get_by_text(self, pattern):
        return Locator(self, 'restriction')
    def get_by_role(self, role, **kwargs):
        require(role == 'button', 'unexpected DOM role')
        return Locator(self, 'next')


class Fixture:
    def __init__(self, directory, name):
        self.path = directory / (name + '.sqlite3')
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user('offline-' + name, 'isolated fixture password only')['id']
        self.other = self.service.register_user('other-' + name, 'isolated fixture password only')['id']
        self.clock = Clock()
        self._evidence_reader = None
        self.pages, self.closed, self.connected, self.disconnected, self.tokens = {}, [], [], [], {}
        self.modes = {}
        self.started = asyncio.Event()
        self.release = None
        self.manager = StudioManager(self.service, self)
        # Never use a real Desktop folder, even while constructing media helpers.
        self.manager.media.files.root = directory / (name + '-synthetic-media')
    def job(self, profile):
        with self.read_evidence() as connection:
            row=connection.execute('SELECT * FROM studio_jobs WHERE profile_id=? ORDER BY rowid DESC LIMIT 1',(profile,)).fetchone()
        require(row is not None, 'missing per-window job')
        return dict(row)
    @contextmanager
    def read_evidence(self):
        reader=self._evidence_reader
        if reader is None:
            with self.database.read() as connection:yield connection
        else:
            connection,lock=reader
            with lock:yield connection
    def assert_owned(self, profile):
        row = self.job(profile)
        with self.read_evidence() as connection:
            leases=[dict(row) for row in connection.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(profile,))]
        require(len(leases) == 1 and leases[0]['entity_id'] == row['id']
                and leases[0]['operation_type'] == 'studio', 'effect lacks the matching production lease')
        token = leases[0]['lease_token']
        require(self.tokens.setdefault(profile, token) == token, 'lease generation changed during one execution')
    def close_profile(self, profile):
        self.assert_owned(profile)
        require(profile in self.disconnected, 'provider close preceded disconnect')
        require(self.job(profile)['status'] == 'completed', 'provider closed an unfinished window')
        self.closed.append(profile)
        return {'closed': True}
    def worker(self, provider):
        require(provider is self, 'wrong synthetic provider')
        fixture = self
        class Worker:
            async def connect(self, profile, **kwargs):
                fixture.assert_owned(profile)
                fixture.connected.append(profile)
                self.profile = profile
                self.page = Page(fixture, profile, fixture.modes.get(profile, 'normal'))
                self._context = self.page
                fixture.pages[profile] = self.page
                fixture.clock.advance(2.)
                fixture.started.set()
                if fixture.release is not None:
                    await fixture.release.wait()
            async def open_posting_page(self):
                fixture.assert_owned(self.profile)
                self.page.events.append('open-owned-tab')
            async def _guard(self):
                fixture.assert_owned(self.profile)
            @asynccontextmanager
            async def _destructive_action_lease(self):
                fixture.assert_owned(self.profile)
                yield
                fixture.assert_owned(self.profile)
            async def disconnect(self):
                fixture.assert_owned(self.profile)
                fixture.disconnected.append(self.profile)
                self.page.disconnected = True
        return Worker()
    async def start(self, profiles, *, config=None, request='offline-start-request'):
        return (await self.manager.command(self.owner, {'action': 'start', 'kind': 'nurture',
            'request_id': request, 'profile_ids': profiles, 'config': config or {}}))['job_ids']
    async def execute(self, ident):
        self.manager.gates[ident] = asyncio.Event()
        self.manager.gates[ident].set()
        await self.manager._execute(self.manager.get(self.owner, ident))
    def patches(self):
        stack = ExitStack()
        # Evidence polls used to reopen and reparse the full SQLite schema on
        # every fake DOM/guard read. Reuse only this fixture's read-only observer,
        # in autocommit so every assertion sees the latest committed state.
        # The production manager/Database, FULL-sync writes, sampling cadence,
        # and every assertion are unchanged. Close before explicit restart proof.
        observer = stack.enter_context(closing(sqlite3.connect(self.path,timeout=30.0,
            isolation_level=None,check_same_thread=False)))
        observer.row_factory=sqlite3.Row
        observer.execute('PRAGMA query_only=ON')
        stack.enter_context(patch.object(self,'_evidence_reader',(observer,threading.RLock())))
        stack.enter_context(patch.object(studio, 'PlaywrightWorker', self.worker))
        stack.enter_context(patch.object(studio, 'time', SimpleNamespace(monotonic=self.clock.now)))
        stack.enter_context(patch.object(studio, 'random', SimpleNamespace(randint=lambda low, high: low)))
        stack.enter_context(patch.object(standalone_nurture, 'asyncio',
            SimpleNamespace(sleep=self.clock.sleep, CancelledError=asyncio.CancelledError)))
        stack.enter_context(patch(__package__ + '.instagram_home.prepare_instagram_home', new=AsyncMock()))
        # Boundary draws prove .69 is selected, .70 is rejected, and repeated,
        # unknown and already-liked videos do not consume another random draw.
        original_init = standalone_nurture.StandaloneNurture.__init__
        def initialize(engine, browser):
            original_init(engine, browser)
            def draw():
                page = browser.page
                value = (.69, .70)[min(page.draws, 1)]
                page.draws += 1
                return value
            engine.draw = draw
        stack.enter_context(patch.object(standalone_nurture.StandaloneNurture, '__init__', initialize))
        return stack
    def reopen(self):
        self.database = Database(self.path)
        self.database.initialize()
        self.service = CoreService(self.database)
        self.manager = StudioManager(self.service, self)
        self.manager.media.files.root = self.path.parent / 'synthetic-reopened-media'


async def selection_case(directory):
    f = Fixture(directory, 'selection')
    default = config_for('nurture', {})
    require(default['minutes'] == 5 and default['concurrency'] == 0, 'default minutes or all-selected concurrency changed')
    fixed = config_for('nurture', {'minutes': 2, 'concurrency': 3, 'dwell_min': 99,
        'dwell_max': 100, 'like_probability': 1, 'surfaces': ['feed'], 'rounds': 7,
        'save_probability': 100, 'follow_probability': 100, 'comment_probability': 100})
    require(fixed['minutes'] == 2 and fixed['concurrency'] == 3, 'editable settings discarded')
    require(fixed['surfaces'] == ['reels'] and fixed['dwell_min'] == 8 and fixed['dwell_max'] == 20
            and fixed['like_probability'] == 70 and fixed['rounds'] == 1
            and all(fixed[name + '_probability'] == 0 for name in ('save', 'follow', 'comment')),
            'legacy settings escaped the fixed standalone policy')
    for config in ({'minutes': True}, {'minutes': 0}, {'minutes': 121}, {'minutes': 1.5},
                   {'concurrency': True}, {'concurrency': -1}, {'concurrency': 1001}):
        try:
            config_for('nurture', config)
        except ValidationError:
            pass
        else:
            raise RuntimeError('Invalid standalone input was admitted: ' + repr(config))
    steps = build_nurture_steps(default)
    require(sum(step['seconds'] for step in steps) == 300 and all(8 <= step['seconds'] <= 20 for step in steps),
            'default plan duration/dwell bounds changed')
    blocker = f.service.acquire_browser_lease(f.owner, 'blocked', operation_type='monitor', entity_id='external', ttl_seconds=600)
    before = records(f.database, 'studio_jobs')
    try:
        await f.start(['free', 'blocked'], request='blocked-batch-request')
    except ConflictError:
        pass
    else:
        raise RuntimeError('Occupied selected window was admitted')
    require(records(f.database, 'studio_jobs') == before, 'blocked selected batch partially created jobs')
    require(records(f.database, 'browser_operation_leases')[0]['lease_token'] == blocker, 'blocked batch touched another lease')
    f.service.release_browser_lease('blocked', blocker)
    with f.patches():
        ids = await f.start(['selected-one', 'selected-two', 'selected-one'], config={'minutes': 1})
        require(len(ids) == 2, 'selected-window deduplication failed')
        require(await f.start(['selected-one', 'selected-two', 'selected-one'], config={'minutes': 1}) == ids,
                'same start request created another job')
        require(all(json.loads(f.manager.get(f.owner, ident)['config_json'])['concurrency'] == 2 for ident in ids),
                'zero concurrency was not resolved to every selected window')
        f.release = asyncio.Event()
        f.manager._schedule_ready()
        for _ in range(100):
            if len(f.connected) == 2:
                break
            await asyncio.sleep(.01)
        require(set(f.connected) == {'selected-one', 'selected-two'} and len(f.manager.active_ids()) == 2,
                'scheduler did not start exactly every selected window concurrently')
        require(len(records(f.database, 'browser_operation_leases')) == 2, 'concurrent selected jobs lack distinct leases')
        f.release.set()
        await asyncio.wait_for(asyncio.gather(*list(f.manager.tasks.values())), 30)
    for ident in ids:
        require(f.manager.get(f.owner, ident)['status'] == 'completed', 'selected job did not complete')
    require(sorted(f.closed) == ['selected-one', 'selected-two'], 'completed close policy changed')
    require(not records(f.database, 'browser_operation_leases'), 'completed selected leases remain')
    return {'verified': True, 'default_minutes': 5, 'default_concurrency': 0, 'selected_windows': 2,
            'concurrent_windows': 2, 'blocked_batch_atomic': True, 'foreign_lease_untouched': True,
            'fixed_policy_normalized': True, 'editable_minutes_and_concurrency': True,
            'request_idempotent': True, 'only_selected_windows_executed': True}


async def completed_case(directory):
    f = Fixture(directory, 'completed')
    f.modes['history-window'] = 'plural_routes'
    with f.patches():
        ident = (await f.start(['history-window'], config={'minutes': 1}))[0]
        await f.execute(ident)
    row = f.manager.get(f.owner, ident)
    result = json.loads(row['result_json'])
    page = f.pages['history-window']
    require(row['status'] == 'completed' and result['nurture_outcome'] == 'completed', 'history outcome not completed')
    require(52 <= result['nurture_actual_seconds'] <= 60.001, 'actual active duration was not bounded by one edited minute')
    require(result['nurture_actual_seconds'] != 0 and abs(result['nurture_actual_seconds']-result['counts']['browse_seconds'])<.002,
            'actual duration counted non-playback startup, navigation or confirmation time')
    require(result['account_snapshot']['posts_count'] == 0 and result['account_snapshot']['followers_count'] == 1234
            and result['account_snapshot']['following_count'] == 27 and result['account_snapshot']['status'] == 'ok',
            'first verified own-profile metrics were lost or fabricated')
    require(page.draws == 2 and page.clicked == ['/reel/A'], '70 percent threshold or stable-video one-decision rule changed')
    require(result['nurture_decisions']['/reel/A']['selected'] is True
            and result['nurture_decisions']['/reel/B']['selected'] is False
            and result['nurture_decisions']['/reel/C']['state'] == 'unknown'
            and result['nurture_decisions']['/reel/D']['state'] == 'liked', 'durable per-video decisions incomplete')
    require(result['nurture_actions']['like:/reel/A']['state'] == 'confirmed' and result['counts']['like'] == 1,
            'confirmed action was lost or double counted')
    require(f.manager.daily_action_counts(f.owner, 'history-window')['like'] == 1, 'daily count differs from confirmed history')
    require(f.closed == ['history-window'] and f.disconnected == ['history-window'], 'success cleanup missing')
    require(page.route_forms == {'singular', 'plural'}, 'both canonical Reels shortcode routes were not exercised')
    require(not records(f.database, 'browser_operation_leases'), 'success lease leaked')
    snapshot = f.manager.snapshot(f.owner)
    require(f.manager.snapshot(f.other)['jobs'] == [] and f.manager.snapshot(f.other)['window_stats'] == [], 'history crossed owner boundary')
    stats = snapshot['window_stats'][0]
    require(stats['nurture_count'] == 1 and stats['last_nurture_at'] == result['nurture_started_at'], 'per-window run count or last time missing')
    f.reopen()
    require(f.manager.snapshot(f.owner) == snapshot, 'per-window history changed after database reopen')
    require(json.loads(f.manager.get(f.owner, ident)['result_json']) == result, 'runtime receipts changed after database reopen')
    # A second run must append history, never overwrite the first run's snapshot.
    f.modes['history-window'] = 'partial_counts'
    f.tokens.clear()
    with f.patches():
        second = (await f.start(['history-window'], config={'minutes': 1}, request='second-history-request'))[0]
        await f.execute(second)
    next_result = json.loads(f.manager.get(f.owner, second)['result_json'])
    require(next_result['account_snapshot']['following_count'] is None
            and next_result['account_snapshot']['status'] == 'partial', 'unknown compact count fabricated as an exact number')
    require(json.loads(f.manager.get(f.owner, ident)['result_json']) == result, 'later run overwrote first history')
    stats = f.manager.snapshot(f.owner)['window_stats'][0]
    require(stats['nurture_count'] == 2 and len(f.manager.snapshot(f.owner)['jobs']) == 2, 'later per-window run did not append history')
    resume = await safe_resume_case(directory)
    return {'verified': True, **resume, 'actual_seconds': result['nurture_actual_seconds'],
            'planned_seconds': 60, 'actual_duration_recorded': True, 'active_budget_enforced': True,
            'first_verified_snapshot_before_reels': True, 'first_verified_snapshot_immutable': True,
            'followers': 1234, 'following': 27, 'posts': 0, 'unknown_counts_remain_unknown': True,
            'probability_draws': 2, 'synthetic_like_clicks': 1, 'one_decision_per_stable_video': True,
            'exact_70_percent_boundary': True, 'unknown_and_liked_never_clicked': True,
            'confirmed_history_atomic': True, 'database_reopen_persistence': True,
            'history_owner_isolation': True, 'per_window_runs': 2, 'close_after_disconnect': True,
            'same_lease_through_cleanup': True, 'completed_lease_released': True,
            'canonical_singular_and_plural_routes': True}


async def safe_resume_case(directory):
    f = Fixture(directory, 'safe-resume')
    f.modes['resume-window'] = 'pause_after_confirmed'
    with f.patches():
        ident = (await f.start(['resume-window'], config={'minutes': 1}))[0]
        await f.execute(ident)
    paused = f.manager.get(f.owner, ident)
    original = json.loads(paused['result_json'])
    require(paused['status'] == 'paused' and paused['inflight'] == 0, 'safe interruption did not pause resumably')
    require(original['nurture_actions']['like:/reel/A']['state'] == 'confirmed', 'safe pause lost confirmed receipt')
    require(not f.closed and len(records(f.database, 'browser_operation_leases')) == 1, 'safe pause lost original hold policy')
    require(8 <= original['nurture_actual_seconds'] < 9, 'safe pause recorded planned rather than actual duration')
    f.reopen()
    f.manager.recover()
    f.tokens.clear()
    f.modes['resume-window'] = 'resumed_counts'
    await f.manager.control(f.owner, ident, 'resume')
    with f.patches():
        await f.execute(ident)
    row = f.manager.get(f.owner, ident)
    resumed = json.loads(row['result_json'])
    require(row['status'] == 'completed' and original['nurture_actual_seconds'] < resumed['nurture_actual_seconds'] <= 60.001,
            'resume reset or exceeded the active duration budget')
    require(resumed['account_snapshot'] == original['account_snapshot'], 'resume replaced the first verified own-profile read')
    require(len(resumed['account_snapshots']) == 2 and resumed['account_snapshots'][-1]['following_count'] == 28,
            'resume failed to keep its distinct verified observation')
    require('/reel/A' not in f.pages['resume-window'].clicked, 'resume replayed a previously confirmed like')
    require(resumed['nurture_decisions']['/reel/A'] == original['nurture_decisions']['/reel/A'], 'resume rerolled the same video decision')
    require(f.manager.snapshot(f.owner)['window_stats'][0]['nurture_count'] == 1, 'one resumed run counted as two per-window runs')
    require(f.closed == ['resume-window'] and not records(f.database, 'browser_operation_leases'), 'resumed success did not clean up once')
    return {'safe_resume_preserves_first_verified_snapshot': True, 'resume_observations': 2,
            'resume_no_confirmed_effect_replay': True, 'resume_accumulates_active_duration': True,
            'resume_counts_as_one_run': True}


async def unknown_owner_case(directory):
    evidence = {}
    for mode in ('unknown_owner', 'identity_changed'):
        f = Fixture(directory, mode)
        f.modes['unknown-window'] = mode
        with f.patches():
            ident = (await f.start(['unknown-window'], config={'minutes': 1}))[0]
            await f.execute(ident)
        row = f.manager.get(f.owner, ident)
        page = f.pages['unknown-window']
        require(row['status'] == 'failed' and not row['inflight'], 'unknown own identity was not failed closed')
        require('goto:' + standalone_nurture.REELS_URL not in page.events and not page.clicked, 'unknown own identity reached Reels/effect')
        require(not f.closed and f.disconnected == ['unknown-window'], 'failed own-profile run changed original close policy')
        require(not records(f.database, 'browser_operation_leases'), 'failed own-profile run leaked lease')
        result = json.loads(row['result_json'])
        require(result['nurture_outcome'] == 'failed' and result['nurture_actual_seconds'] == 0, 'unverified preflight was counted as active Reels time')
        evidence[mode] = True
    return {'verified': True, **evidence, 'no_reels_or_effect': True, 'failed_window_retained': True,
            'failed_lease_released': True, 'failed_actual_duration_and_outcome': True}


async def interrupted_case(directory):
    f = Fixture(directory, 'interrupted')
    f.modes['pending-window'] = 'interrupted'
    with f.patches():
        ident = (await f.start(['pending-window'], config={'minutes': 1}))[0]
        await f.execute(ident)
    row = f.manager.get(f.owner, ident)
    result = json.loads(row['result_json'])
    require(row['status'] == 'needs_review' and row['inflight'] == 1, 'interrupted effect lost pending fence')
    require(result['nurture_actions']['like:/reel/A']['state'] == 'pending' and result.get('counts', {}).get('like', 0) == 0,
            'interrupted synthetic click incorrectly confirmed')
    require(result['nurture_outcome'] == 'needs_review' and result['nurture_actual_seconds'] > 0, 'pending run lacks actual time/outcome')
    require(not f.closed and len(records(f.database, 'browser_operation_leases')) == 1, 'pending effect did not retain window lease')
    require(f.pages['pending-window'].clicked == ['/reel/A'], 'interrupted click count changed')
    try:
        await f.manager.control(f.owner, ident, 'resume')
    except ConflictError:
        pass
    else:
        raise RuntimeError('Pending action resumed without review')
    f.reopen()
    f.manager.recover()
    require(f.manager.get(f.owner, ident)['status'] == 'needs_review', 'restart discarded pending status')
    with patch.object(studio, 'PlaywrightWorker', side_effect=AssertionError('Pending effect reopened a browser')):
        await f.execute(ident)
    recovered = json.loads(f.manager.get(f.owner, ident)['result_json'])
    require(recovered['nurture_actions'] == result['nurture_actions']
            and recovered['nurture_decisions'] == result['nurture_decisions'], 'restart rewrote durable effect receipts')
    require(recovered['account_snapshot'] == result['account_snapshot'], 'restart replaced the first snapshot')
    require(len(records(f.database, 'browser_operation_leases')) == 1, 'restart dropped required review lease')
    return {'verified': True, 'synthetic_click_attempts': 1, 'pending_committed_before_click': True,
            'needs_review_after_interruption': True, 'resume_without_review_rejected': True,
            'restart_does_not_open_browser_or_replay': True, 'decisions_and_snapshot_persist': True,
            'pending_window_and_lease_retained': True, 'actual_duration_and_outcome_retained': True}


async def legacy_case(directory):
    f = Fixture(directory, 'legacy')
    ident = (await f.start(['legacy-window'], config={'minutes': 1}))[0]
    row = f.manager.get(f.owner, ident)
    config = json.loads(row['config_json'])
    config.pop('standalone_policy')
    f.manager.update(ident, config_json=json.dumps(config))
    with patch.object(studio, 'PlaywrightWorker', side_effect=AssertionError('Legacy job opened a browser')):
        await f.execute(ident)
    require(f.manager.get(f.owner, ident)['status'] == 'paused', 'unmarked legacy plan was silently executed')
    for action in ('resume', 'retry'):
        try:
            await f.manager.control(f.owner, ident, action)
        except ValidationError:
            pass
        else:
            raise RuntimeError('Legacy plan control escaped fixed-policy fence')
    require(not records(f.database, 'browser_operation_leases'), 'legacy job acquired a lease')
    require(json.loads(f.manager.get(f.owner, ident)['config_json']) == config, 'legacy history was rewritten')
    timed=(await f.start(['legacy-clock-window'],config={'minutes':1},request='legacy-clock-fixture'))[0]
    old={'nurture_actual_seconds':59,'counts':{'like':1},'nurture_started_at':'2026-10-02T10:00:00Z'}
    f.manager.update(timed,result_json=json.dumps(old))
    with patch.object(studio,'PlaywrightWorker',side_effect=AssertionError('Legacy wall clock opened a browser')):
        await f.execute(timed)
    old_saved=json.loads(f.manager.get(f.owner,timed)['result_json'])
    require(f.manager.get(f.owner,timed)['status']=='paused' and old_saved['nurture_actual_seconds']==59
            and old_saved['counts']==old['counts'] and 'confirmed_at' not in old_saved,
            'legacy elapsed time silently became verified playback or lost history')
    return {'verified': True, 'unmarked_legacy_jobs_fail_closed': True, 'resume_and_retry_rejected': True,
            'legacy_history_preserved': True, 'no_browser_or_lease': True,'legacy_wall_time_not_reinterpreted':True}


async def verified_playback_case(directory):
    for mode in ('frozen_playback','buffering','advance_noop','keyboard_next'):
        f=Fixture(directory,'watch-'+mode)
        profile='watch-window';f.modes[profile]=mode
        with f.patches():
            ident=(await f.start([profile],config={'minutes':1}))[0]
            await f.execute(ident)
        row=f.manager.get(f.owner,ident);result=json.loads(row['result_json']);page=f.pages[profile]
        require(result.get('nurture_clock_policy')=='verified-playback-v2','old wall-time clock still used')
        if mode=='frozen_playback':
            require(row['status']=='failed' and result['nurture_actual_seconds']==0,'frozen video earned watch time or completed')
            require(page.clicked==[] and f.closed==[],'stalled media caused an interaction or successful close')
        elif mode=='advance_noop':
            require(row['status']=='failed' and page.events.count('next-noop')==1,'no-op next was retried or accepted')
            require(8<=result['nurture_actual_seconds']<=20,'failed advance accrued navigation time')
        else:
            require(row['status']=='completed' and abs(result['nurture_actual_seconds']-60)<.002,'verified playback did not fill the edited minute')
            require(abs(result['counts']['browse_seconds']-60)<.002,'browse/watch effective durations disagree')
            if mode=='keyboard_next':require(page.events.count('key:ArrowDown')>1,'icon-only next did not exercise native-key path')
            require(f.closed==[profile] and not records(f.database,'browser_operation_leases'),'verified complete flow did not retire owned window')
    return {'verified':True,'frozen_media_zero_seconds':True,'stalls_excluded_from_duration':True,
            'effective_seconds':60,'startup_navigation_confirmation_excluded':True,
            'stable_advance_required':True,'one_action_without_blind_retry':True,'icon_only_key_advance':True}


async def completed_cleanup_case(directory):
    f=Fixture(directory,'cleanup-fence');profile='cleanup-window'
    with f.patches(),patch.object(studio,'close_profile_and_wait',new=AsyncMock(side_effect=RuntimeError('synthetic cleanup failure'))):
        ident=(await f.start([profile],config={'minutes':1}))[0]
        await f.execute(ident)
    row=f.manager.get(f.owner,ident);before=json.loads(row['result_json']);token=before['window_cleanup']['lease_token']
    require(row['status']=='completed' and before['window_hold'] is True and before['window_cleanup']['state']=='pending','close failure did not retain completed cleanup fence')
    require(records(f.database,'browser_operation_leases')[0]['lease_token']==token,'close failure released exact lease')
    original_clicks=list(f.pages[profile].clicked)
    f.reopen();f.service.recover_interrupted_operations();f.manager.recover()
    require(records(f.database,'browser_operation_leases')[0]['lease_token']==token,'restart replaced or released cleanup token')
    try:await f.start([profile],request='blocked-unclosed-window')
    except ConflictError:pass
    else:raise RuntimeError('Completed-but-unclosed window admitted a new task')
    with patch.object(studio,'PlaywrightWorker',side_effect=AssertionError('Cleanup reopened activity browser')):
        f.manager._schedule_ready()
        await asyncio.gather(*list(f.manager.tasks.values()))
    after=json.loads(f.manager.get(f.owner,ident)['result_json'])
    require(after['window_hold'] is False and after['window_cleanup']['state']=='closed','confirmed retry did not finish cleanup')
    require(after['counts']==before['counts'] and after['confirmed_at']==before['confirmed_at'] and f.pages[profile].clicked==original_clicks,'cleanup replayed effects or changed completed history')
    require(not records(f.database,'browser_operation_leases') and f.closed==[profile],'confirmed owned cleanup did not release exact lease once')
    # A replacement generation is never authority to close the old browser.
    g=Fixture(directory,'cleanup-lost')
    with g.patches(),patch.object(studio,'close_profile_and_wait',new=AsyncMock(side_effect=RuntimeError('synthetic cleanup failure'))):
        other=(await g.start([profile],config={'minutes':1}))[0]
        await g.execute(other)
    pending=json.loads(g.manager.get(g.owner,other)['result_json']);old=pending['window_cleanup']['lease_token']
    with g.database.write() as c:
        c.execute("UPDATE browser_operation_leases SET lease_token='replacement',operation_type='monitor',entity_id='other-task' WHERE profile_id=?",(profile,))
    await g.manager._finish_nurture_cleanup(g.owner,other,profile,old)
    lost=json.loads(g.manager.get(g.owner,other)['result_json'])
    require(lost['window_hold'] is True and lost['window_cleanup']['state']=='lease_lost' and g.closed==[],'lost generation closed a replacement window')
    require(records(g.database,'browser_operation_leases')[0]['lease_token']=='replacement','lost cleanup released another lease')
    return {'verified':True,'close_failure_retains_exact_lease':True,'restart_preserves_cleanup_token':True,
            'new_task_blocked_until_confirmed_close':True,'cleanup_retry_without_activity_replay':True,
            'confirmed_close_releases_once':True,'completed_history_unchanged':True,
            'lost_generation_never_closes_or_releases_replacement':True}


async def proof_stage(name,operation,directory):
    started=time.perf_counter()
    print('STANDALONE_NURTURE_STAGE='+json.dumps({'case':name,'state':'started'},sort_keys=True),flush=True)
    try:result=await operation(directory)
    except BaseException as exc:
        print('STANDALONE_NURTURE_STAGE='+json.dumps({'case':name,'state':'failed',
            'elapsed_seconds':round(time.perf_counter()-started,3),'error_type':type(exc).__name__},sort_keys=True),flush=True)
        raise
    print('STANDALONE_NURTURE_STAGE='+json.dumps({'case':name,'state':'passed',
        'elapsed_seconds':round(time.perf_counter()-started,3)},sort_keys=True),flush=True)
    return result


async def run_selftest():
    def forbidden(*args, **kwargs):
        raise RuntimeError('Offline standalone nurture proof attempted network access')
    with tempfile.TemporaryDirectory(prefix='Juxin-StandaloneNurture-') as temporary, ExitStack() as guard:
        for name in ('connect', 'connect_ex', 'sendto'):
            guard.enter_context(patch.object(socket.socket, name, forbidden))
        for name in ('create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex'):
            guard.enter_context(patch.object(socket, name, forbidden))
        directory = Path(temporary)
        # Do not resolve the user's redirected Windows Desktop or load any
        # configured media credentials, including on deliberately failed jobs.
        guard.enter_context(patch(__package__ + '.studio_files.desktop_root', return_value=directory / 'synthetic-desktop'))
        guard.enter_context(patch(__package__ + '.studio_media.StudioMedia.pexels_key', return_value=''))
        guard.enter_context(patch(__package__ + '.studio_media.StudioMedia.ai_key', return_value=''))
        cases={}
        for name,operation in (('selection_and_fixed_policy',selection_case),
            ('completed_history',completed_case),('unknown_own_profile',unknown_owner_case),
            ('interrupted_pending_effect',interrupted_case),('legacy_plan_fence',legacy_case),
            ('verified_playback',verified_playback_case),('completed_cleanup_fence',completed_cleanup_case)):
            cases[name]=await proof_stage(name,operation,directory)
    return {'verified': True, 'policy': POLICY, 'synthetic': True, 'network_disabled': True,
            'user_data_touched': False, 'live_accounts_tested': False,
            'production_manager_and_service': True, 'production_nurture_engine': True,
            'synthetic_dom_not_live_site_verification': True, 'cases': cases}


def main():
    print(PROOF_PREFIX + json.dumps(asyncio.run(run_selftest()), sort_keys=True))
