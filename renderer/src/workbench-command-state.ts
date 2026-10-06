import type { CollectorCoreClient } from "./core-client";

export type CollectionSafetyRequest =
  | { scope: "task"; taskId: string; action: "pause" }
  | { scope: "window"; taskId: string; profileId: string; targetId: string; action: "pause" | "stop" };

export type ActionSafetyRequest =
  | { scope: "campaign"; campaignId: string; action: "pause" | "stop" }
  | { scope: "target"; campaignId: string; targetId: string; action: "pause" | "cancel" };

/** Existing follow/greeting work must remain stoppable during a failed read. */
export async function performActionSafetyControl(
  client: Pick<CollectorCoreClient, "controlActionCampaign" | "controlActionTarget">,
  request: ActionSafetyRequest,
): Promise<unknown> {
  const nonempty = (value: unknown) => typeof value === 'string' && value.trim().length > 0;
  if (!request || !nonempty(request.campaignId)
    || (request.scope === 'campaign' ? !['pause', 'stop'].includes(request.action)
      : request.scope !== 'target' || !nonempty(request.targetId)
        || !['pause', 'cancel'].includes(request.action))) {
    throw new Error('暂停或取消请求缺少当前任务绑定，请刷新后重试');
  }
  return request.scope === 'campaign'
    ? client.controlActionCampaign(request.campaignId, request.action)
    : client.controlActionTarget(request.campaignId, request.targetId, request.action);
}

/** Only controls that reduce existing work may operate during a failed snapshot. */
export async function performCollectionSafetyControls(
  client: Pick<CollectorCoreClient, "controlCollectionTask" | "controlCollectionWindow">,
  requests: readonly CollectionSafetyRequest[],
): Promise<void> {
  const nonempty = (value: unknown) => typeof value === "string" && value.trim().length > 0;
  // Validate the whole batch before issuing its first command. Never infer a
  // target from a window alone: the backend must compare the expected target.
  for (const request of requests) {
    if (!request || !nonempty(request.taskId)
      || (request.scope === "task" ? request.action !== "pause"
        : request.scope !== "window" || !nonempty(request.profileId) || !nonempty(request.targetId)
          || (request.action !== "pause" && request.action !== "stop"))) {
      throw new Error("暂停或停止请求缺少当前任务绑定，请刷新后重试");
    }
  }
  const failures: string[] = [];
  for (const request of requests) {
    try {
      if (request.scope === "task") await client.controlCollectionTask(request.taskId, "pause");
      else await client.controlCollectionWindow(request.taskId, request.profileId, request.action, request.targetId);
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : String(reason);
      failures.push(`${request.taskId}${request.scope === "window" ? ` / ${request.profileId}` : ""}：${detail}`);
    }
  }
  if (failures.length) throw new Error(`已处理 ${requests.length} 项控制请求，其中 ${failures.length} 项未成功：${failures.join("；")}`);
}

/** Pending means a real command, not a failed or still-refreshing snapshot. */
export function workbenchCommandFeedback(snapshotFailed: boolean, busy: boolean, pending: boolean) {
  return {
    disabled: snapshotFailed || busy,
    pending,
    refreshing: busy && !pending && !snapshotFailed,
    safetyControlDisabled: busy,
  };
}

export async function runWithSnapshotRefresh<T>(options: {
  action: () => Promise<T>;
  refresh: () => Promise<unknown>;
  onCommandSettled: () => void;
  backgroundRefresh?: boolean;
}): Promise<T> {
  try {
    return await options.action();
  } finally {
    options.onCommandSettled();
    // Refresh failure must not replace the command's actual outcome. The
    // authoritative poller reports it separately and continues retrying.
    const refresh = async () => { try { await options.refresh(); } catch { /* poller owns snapshot error */ } };
    if (options.backgroundRefresh) void refresh();
    else await refresh();
  }
}
