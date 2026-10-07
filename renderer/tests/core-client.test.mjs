import assert from "node:assert/strict";
import {
  CollectorCoreProtocolError,
  CollectorCoreUnavailableError,
  createCollectorCoreClient,
  executeWorkbenchCommand,
  fetchWorkbenchSnapshot,
  startWorkbenchSnapshotPolling,
} from "../src/core-client.ts";

function validSnapshot(revision = 1) {
  return {
    revision,
    generated_at: "2026-08-30T10:00:00Z",
    counts: { pending_public: 1, pending_private: 0 },
    dedupe: { total: 42, claimed: 42 },
    pending: { public: [], private: [] },
    approved: { public: [], private: [] },
    history: { manual_rejections: [], collection_exclusions: [] },
    windows: [],
    sources: [],
    tasks: [],
    campaigns: [],
    truncated: false,
  };
}

function bridge(request) {
  return {
    request,
    secureSet: async () => true,
    secureGet: async () => null,
    secureDelete: async () => true,
    configureIntegrations: async () => ({ restarted: true }),
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

{
  assert.throws(
    () => fetchWorkbenchSnapshot({}, undefined),
    (error) => error instanceof CollectorCoreUnavailableError
      && error.code === "collector_core_bridge_unavailable",
    "production must fail closed when preload did not expose collectorCore",
  );
}

{
  const calls = [];
  const fake = bridge(async (path, options) => {
    calls.push({ path, options });
    return validSnapshot(7);
  });
  const client = createCollectorCoreClient(fake);
  const first = client.snapshot({ limit: 120, historyLimit: 80 });
  const duplicate = client.snapshot({ limit: 120, historyLimit: 80 });
  const differentLimits = client.snapshot({ limit: 999, historyLimit: 999 });
  assert.strictEqual(first, duplicate, "identical snapshot queries must share one Core request");
  assert.notStrictEqual(first, differentLimits, "different snapshot limits must never share an in-flight response");
  const [snapshot, secondSnapshot] = await Promise.all([first, differentLimits]);
  assert.equal(snapshot.revision, 7);
  assert.equal(secondSnapshot.revision, 7);
  assert.deepEqual(calls, [
    { path: "/api/workbench/snapshot?limit=120&history_limit=80", options: undefined },
    { path: "/api/workbench/snapshot?limit=999&history_limit=999", options: undefined },
  ]);
}

{
  let revision = 12;
  let requests = 0;
  const client = createCollectorCoreClient(bridge(async () => {
    requests += 1;
    return validSnapshot(revision);
  }));
  assert.equal((await client.snapshot()).revision, 12);
  revision = 11;
  await assert.rejects(
    () => client.snapshot(),
    (error) => error instanceof CollectorCoreProtocolError
      && /stale snapshot revision 11/.test(error.message)
      && /after 4 attempts/.test(error.message),
    "a Core that remains behind the applied revision must fail after bounded retries",
  );
  assert.equal(requests, 5, "one accepted read plus four stale attempts must be bounded");
}

{
  const lateOlder = deferred();
  const newer = deferred();
  const calls = [];
  let olderPathCalls = 0;
  const olderPath = "/api/workbench/snapshot?limit=100&history_limit=500";
  const newerPath = "/api/workbench/snapshot?limit=200&history_limit=500";
  const fake = bridge(async (path) => {
    calls.push(path);
    if (path === olderPath) {
      olderPathCalls += 1;
      return olderPathCalls === 1 ? lateOlder.promise : validSnapshot(32);
    }
    if (path === newerPath) return newer.promise;
    throw new Error(`unexpected path: ${path}`);
  });
  const client = createCollectorCoreClient(fake);
  const lateRequest = client.snapshot({ limit: 100 });
  const lateDuplicate = client.snapshot({ limit: 100 });
  const newerRequest = client.snapshot({ limit: 200 });
  assert.strictEqual(lateRequest, lateDuplicate, "stale recovery must preserve per-query single-flight requests");

  newer.resolve(validSnapshot(31));
  assert.equal((await newerRequest).revision, 31);
  lateOlder.resolve(validSnapshot(30));
  assert.equal(
    (await lateRequest).revision,
    32,
    "a legitimately late response must be discarded and replaced by a fresh snapshot",
  );
  assert.equal(client.latestAppliedSnapshotRevision, 32);
  assert.deepEqual(calls, [olderPath, newerPath, olderPath]);
}

{
  const latePreCommand = deferred();
  let snapshotCalls = 0;
  const fake = bridge(async (path) => {
    if (path === "/api/workbench/commands") {
      return { command: "bitbrowser_refresh", result: { windows: [] }, snapshot_seq: 40 };
    }
    snapshotCalls += 1;
    return snapshotCalls === 1 ? latePreCommand.promise : validSnapshot(40);
  });
  const client = createCollectorCoreClient(fake);
  const lateRequest = client.snapshot();
  await client.refreshBitBrowserWindows();
  latePreCommand.resolve(validSnapshot(39));
  assert.equal(
    (await lateRequest).revision,
    40,
    "a pre-command response arriving after snapshot_seq must be retried behind the command fence",
  );
  assert.equal(snapshotCalls, 2);
}

{
  const acceptedBeforeCommand = deferred();
  const commandResponse = deferred();
  const observed = [];
  const errors = [];
  let snapshotCalls = 0;
  const fake = bridge(async (path) => {
    if (path === "/api/workbench/commands") return commandResponse.promise;
    snapshotCalls += 1;
    return snapshotCalls === 1 ? acceptedBeforeCommand.promise : validSnapshot(61);
  });
  const poller = startWorkbenchSnapshotPolling({
    immediate: false,
    intervalMs: 5_000,
    onSnapshot: (snapshot) => observed.push(snapshot.revision),
    onError: (error) => errors.push(error.message),
  }, fake);

  const pendingSnapshot = poller.refresh();
  const pendingCommand = executeWorkbenchCommand("bitbrowser_refresh", {}, fake);
  // Queue the old snapshot continuation first. It is accepted by snapshot()
  // before the command continuation raises the fence, but must be checked once
  // more before the poller is allowed to hand it to React.
  acceptedBeforeCommand.resolve(validSnapshot(60));
  commandResponse.resolve({
    command: "bitbrowser_refresh",
    result: { windows: [] },
    snapshot_seq: 61,
  });
  await pendingCommand;
  assert.equal(
    (await poller.refresh()).revision,
    61,
    "a command-time refresh must not reuse a snapshot accepted before its revision fence",
  );
  assert.equal((await pendingSnapshot).revision, 61);
  assert.deepEqual(observed, [61], "a pre-command revision must never reach onSnapshot after the command completes");
  assert.deepEqual(errors, []);
  assert.equal(snapshotCalls, 2, "the in-flight poll must replace the stale delivery with one fresh Core read");
  poller.stop();
}

{
  const acceptedBeforeCommand = deferred();
  const commandResponse = deferred();
  const events = [];
  const observed = [];
  let snapshotCalls = 0;
  const commandEnvelope = {
    command: "bitbrowser_refresh",
    result: { windows: [] },
  };
  Object.defineProperty(commandEnvelope, "snapshot_seq", {
    get() {
      events.push("fence-read");
      return 71;
    },
  });
  const fake = bridge(async (path) => {
    if (path === "/api/workbench/commands") return commandResponse.promise;
    snapshotCalls += 1;
    return snapshotCalls === 1 ? acceptedBeforeCommand.promise : validSnapshot(71);
  });
  const poller = startWorkbenchSnapshotPolling({
    immediate: false,
    intervalMs: 5_000,
    onSnapshot: (snapshot) => {
      events.push(`snapshot:${snapshot.revision}`);
      observed.push(snapshot.revision);
    },
  }, fake);

  const pendingSnapshot = poller.refresh();
  const pendingCommand = executeWorkbenchCommand("bitbrowser_refresh", {}, fake);
  acceptedBeforeCommand.resolve(validSnapshot(70));
  // Resolve the command in the exact Promise gap that used to exist between
  // readDeliverableSnapshot() returning revision 70 and its .then(onSnapshot)
  // delivery callback.  A delivery-time fence check must replace it with 71.
  let deliveryGap = Promise.resolve();
  for (let index = 0; index < 4; index += 1) {
    deliveryGap = deliveryGap.then(() => undefined);
  }
  void deliveryGap.then(() => commandResponse.resolve(commandEnvelope));

  await Promise.all([pendingSnapshot, pendingCommand]);
  assert.deepEqual(
    observed,
    [71],
    "a command completing in the final Promise gap must fence the already accepted snapshot",
  );
  assert.equal(snapshotCalls, 2, "delivery-time fencing must perform one bounded fresh Core read");
  assert.ok(
    events.indexOf("fence-read") < events.indexOf("snapshot:71"),
    "the completed command fence must be read before the fresh snapshot reaches onSnapshot",
  );
  poller.stop();
}

{
  const fake = bridge(async (path) => path === "/api/workbench/commands"
    ? { command: "bitbrowser_refresh", result: { windows: [] }, snapshot_seq: 20 }
    : validSnapshot(19));
  const client = createCollectorCoreClient(fake);
  await client.refreshBitBrowserWindows();
  await assert.rejects(
    () => client.snapshot(),
    (error) => error instanceof CollectorCoreProtocolError && /minimum accepted revision is 20/.test(error.message),
    "a snapshot older than the latest completed command must fail closed",
  );
}

{
  const calls = [];
  const fake = bridge(async (path, options) => {
    calls.push({ path, options });
    return {
      command: "action_failure_dismiss",
      result: { campaign_id: "campaign-1", target_id: "target-1", status: "dismissed" },
      snapshot_seq: 9,
    };
  });
  const client = createCollectorCoreClient(fake);
  const result = await client.dismissActionFailure("campaign-1", "target-1");
  assert.equal(result.status, "dismissed");
  assert.deepEqual(calls, [{
    path: "/api/workbench/commands",
    options: {
      method: "POST",
      body: {
        type: "action_failure_dismiss",
        payload: { campaign_id: "campaign-1", target_id: "target-1" },
      },
    },
  }]);
}

{
  const calls = [];
  const fake = bridge(async (path, options) => {
    calls.push({ path, options });
    return {
      command: "action_unknown_resolve",
      result: {
        campaign_id: "campaign-unknown",
        target_id: "target-unknown",
        username: "uncertain_account",
        operation: "greet",
        outcome: "completed",
        status: "confirmed",
        returned_to_approved: false,
        success_ledger_recorded: true,
        global_dedupe_retained: true,
        snapshot_seq: 10,
      },
      snapshot_seq: 10,
    };
  });
  const client = createCollectorCoreClient(fake);
  const result = await client.resolveUnknownAction(
    "campaign-unknown",
    "target-unknown",
    "completed",
  );
  assert.equal(result.success_ledger_recorded, true);
  assert.deepEqual(calls, [{
    path: "/api/workbench/commands",
    options: {
      method: "POST",
      body: {
        type: "action_unknown_resolve",
        payload: {
          campaign_id: "campaign-unknown",
          target_id: "target-unknown",
          outcome: "completed",
        },
      },
    },
  }]);
}

{
  const calls = [];
  const retained = {
    global_dedupe: true,
    review_history: true,
    collection_exclusions: true,
    action_success_history: true,
    live_task_checkpoints: true,
    pending_review_previews: true,
  };
  const fake = bridge(async (path, options) => {
    calls.push({ path, options });
    return {
      command: "storage_cache_clear",
      result: {
        cleared_entries: 3,
        cleared_bytes: 8_192,
        last_cleanup_at: "2026-09-01T15:00:00Z",
        business_records_retained: true,
        retained,
        snapshot_seq: 21,
      },
      snapshot_seq: 21,
    };
  });
  const client = createCollectorCoreClient(fake);
  const result = await client.clearStorageCache();
  assert.equal(result.business_records_retained, true);
  assert.deepEqual(result.retained, retained);
  assert.deepEqual(calls, [{
    path: "/api/workbench/commands",
    options: {
      method: "POST",
      body: {
        type: "storage_cache_clear",
        payload: {},
      },
    },
  }]);
}

{
  const hasMore = {
    pending_public_accounts: true,
    pending_private_accounts: false,
    collection_exclusion_history: true,
    tasks: false,
  };
  const fake = bridge(async () => ({ ...validSnapshot(22), has_more: hasMore }));
  const snapshot = await fetchWorkbenchSnapshot({}, fake);
  assert.deepEqual(snapshot.has_more, hasMore, "boolean has_more scopes must survive protocol validation");
}

{
  const fake = bridge(async () => ({
    ...validSnapshot(23),
    has_more: { pending_public_accounts: "yes" },
  }));
  await assert.rejects(
    () => fetchWorkbenchSnapshot({}, fake),
    (error) => error instanceof CollectorCoreProtocolError
      && /has_more\.pending_public_accounts/.test(error.message),
    "a non-boolean has_more scope must fail closed",
  );
}

{
  const fake = bridge(async () => ({ ...validSnapshot(), windows: undefined }));
  await assert.rejects(
    () => fetchWorkbenchSnapshot({}, fake),
    (error) => error instanceof CollectorCoreProtocolError && /windows/.test(error.message),
    "malformed Core data must never be normalized to an empty simulator-like page",
  );
}

{
  const calls = [];
  const fake = bridge(async (path, options) => {
    calls.push({ path, options });
    return {
      command: "review_decision",
      result: {
        candidate_id: "candidate-1",
        decision: "approved",
        visibility: "private",
        destination: "approved_private",
        reviewed_at: "2026-08-30T10:01:00Z",
        candidate: {},
      },
      snapshot_seq: 8,
    };
  });
  const result = await executeWorkbenchCommand(
    "review_decision",
    { candidate_id: "candidate-1", decision: "approved" },
    fake,
  );
  assert.equal(result.destination, "approved_private");
  assert.deepEqual(calls, [{
    path: "/api/workbench/commands",
    options: {
      method: "POST",
      body: {
        type: "review_decision",
        payload: { candidate_id: "candidate-1", decision: "approved" },
      },
    },
  }]);
}

{
  let requests = 0;
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const fake = bridge(async () => {
    requests += 1;
    await pending;
    return validSnapshot(11);
  });
  const observed = [];
  const poller = startWorkbenchSnapshotPolling({
    immediate: false,
    intervalMs: 5_000,
    onSnapshot: (snapshot) => observed.push(snapshot.revision),
  }, fake);
  const first = poller.refresh();
  const second = poller.refresh();
  assert.strictEqual(first, second, "manual refresh during a poll must be single-flight");
  assert.equal(requests, 1);
  release();
  await first;
  assert.deepEqual(observed, [11]);
  assert.equal(poller.current?.revision, 11);
  poller.stop();
  assert.equal(poller.stopped, true);
  await assert.rejects(() => poller.refresh(), /stopped/);
}

{
  const snapshotRevisions = [50, 51, 52];
  const observed = [];
  const errors = [];
  let snapshotCalls = 0;
  const fake = bridge(async (path) => {
    if (path === "/api/workbench/commands") {
      return { command: "bitbrowser_refresh", result: { windows: [] }, snapshot_seq: 52 };
    }
    const revision = snapshotRevisions[snapshotCalls];
    snapshotCalls += 1;
    if (revision === undefined) throw new Error("unexpected extra snapshot request");
    return validSnapshot(revision);
  });
  const poller = startWorkbenchSnapshotPolling({
    immediate: false,
    intervalMs: 5_000,
    onSnapshot: (snapshot) => observed.push(snapshot.revision),
    onError: (error) => errors.push(error.message),
  }, fake);

  assert.equal((await poller.refresh()).revision, 50);
  await executeWorkbenchCommand("bitbrowser_refresh", {}, fake);
  assert.equal(
    (await poller.refresh()).revision,
    52,
    "polling must automatically recover when the first post-command response is stale",
  );
  assert.deepEqual(observed, [50, 52], "the stale revision must never roll back rendered UI state");
  assert.deepEqual(errors, [], "a recoverable late snapshot must not raise the renderer error banner");
  assert.equal(poller.current?.revision, 52);
  assert.equal(poller.stopped, false, "transient response reordering must not stop polling");
  assert.equal(snapshotCalls, 3);
  poller.stop();
}

{
  const calls = [];
  const fake = bridge(async (path, options) => {
    calls.push({ path, options });
    const command = options.body.type;
    return {
      command,
      result: command === "split_waiting_add"
        ? { candidates: [], accepted_ids: [], duplicates: [] }
        : { id: "candidate-1", username: "target", allowed_window_ids: ["window-b", "window-c"] },
      snapshot_seq: calls.length,
    };
  });
  const client = createCollectorCoreClient(fake);
  await client.addWaitingSplitTargets(["target"], false, ["window-b", "window-c"]);
  await client.assignWaitingSplitWindows("candidate-1", ["window-c"]);
  await client.assignWaitingSplitWindows("candidate-1", []);
  assert.deepEqual(calls, [
    {
      path: "/api/workbench/commands",
      options: {
        method: "POST",
        body: {
          type: "split_waiting_add",
          payload: {
            targets: ["target"],
            allow_completed_targets: false,
            allowed_window_ids: ["window-b", "window-c"],
          },
        },
      },
    },
    {
      path: "/api/workbench/commands",
      options: {
        method: "POST",
        body: {
          type: "split_waiting_assign_windows",
          payload: {
            candidate_id: "candidate-1",
            allowed_window_ids: ["window-c"],
          },
        },
      },
    },
    {
      path: "/api/workbench/commands",
      options: {
        method: "POST",
        body: {
          type: "split_waiting_assign_windows",
          payload: {
            candidate_id: "candidate-1",
            allowed_window_ids: [],
          },
        },
      },
    },
  ]);
}

console.log("formal Core renderer adapter: ok");

{
  const calls = [];
  const client = createCollectorCoreClient(bridge(async (path, options) => {
    calls.push({ path, options });
    return { run_id: "check", status: "running", total: 1 };
  }));
  await client.startFollowMonitor(["window-a"], 2);
  await client.startFollowMonitor(["window-b"], 1, "dm");
  assert.deepEqual(calls.map(call => call.options.body), [
    { profile_ids: ["window-a"], concurrency: 2, check_kind: "combined" },
    { profile_ids: ["window-b"], concurrency: 1, check_kind: "dm" },
  ], "default check combines following and DMs while legacy callers remain supported");
  assert.ok(calls.every(call => call.path === "/api/follow-monitor/runs" && call.options.method === "POST"));
}

{
  const calls = [];
  const client = createCollectorCoreClient(bridge(async (path, options) => {
    calls.push({path, options});
    return {run_id: "current-run", status: "paused"};
  }));
  for (const action of ["pause", "resume", "cancel"]) await client.controlFollowMonitor("current-run", action);
  assert.deepEqual(calls.map(call => call.options.body), [
    {run_id: "current-run", action: "pause"}, {run_id: "current-run", action: "resume"}, {run_id: "current-run", action: "cancel"},
  ]);
  assert.ok(calls.every(call => call.path === "/api/follow-monitor/control" && call.options.method === "POST"));
}

{
  const calls=[];
  const fake=bridge(async(path)=>{calls.push(path);return validSnapshot(30)});
  const client=createCollectorCoreClient(fake);
  const compact=client.snapshot({limit:2000,historyLimit:2000,compact:true});
  assert.strictEqual(client.snapshot({limit:2000,historyLimit:2000,compact:true}),compact,'compact requests remain single flight');
  const legacy=client.snapshot({limit:2000,historyLimit:2000});
  assert.notStrictEqual(legacy,compact,'legacy and compact response contracts must not share an in-flight request');
  const [small,full]=await Promise.all([compact,legacy]);
  assert.deepEqual(small,full,'canonical rows, locks, counts, errors and truncation normalize unchanged');
  assert.deepEqual(calls,[
    '/api/workbench/snapshot?limit=2000&history_limit=2000&compact=1',
    '/api/workbench/snapshot?limit=2000&history_limit=2000',
  ]);
  const platforms=[];
  for(const platform of ['instagram']) {
    const scoped=createCollectorCoreClient(bridge(async(path)=>{platforms.push(path);return {...validSnapshot(30),platform}}),platform);
    await scoped.snapshot({limit:2000,historyLimit:2000,compact:true});
  }
  assert.deepEqual(platforms,[
    '/api/workbench/snapshot?limit=2000&history_limit=2000&platform=instagram&compact=1',
  ]);
}

{
  const client = createCollectorCoreClient(bridge(async()=>({})));
  assert.equal('postingSnapshot' in client, false);
  assert.equal('postingCommand' in client, false);
}

