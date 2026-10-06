import type { CoreBitBrowserWindow, CoreCollectionTask, CoreSplitCandidate } from './core-client';

const JOIN_STATES = new Set(['running', 'paused', 'waiting_network']);
export function collectionAssignmentTasks(tasks: CoreCollectionTask[]) {
  return [...tasks].filter(task => task.settings?.live_queue_enabled === true && JOIN_STATES.has(String(task.status).toLowerCase()))
    .sort((a, b) => String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || '')));
}

export function assignmentSourceTask(tasks: CoreCollectionTask[], candidate?: CoreSplitCandidate) {
  if (!candidate?.source_target_id || candidate.requires_source_task === false) return undefined;
  return tasks.find(task => task.id === candidate.source_task_id && Boolean(task.windows?.length));
}

export function assignmentTaskScope(tasks: CoreCollectionTask[], candidate?: CoreSplitCandidate) {
  const source = assignmentSourceTask(tasks, candidate);
  return source ? [source] : collectionAssignmentTasks(tasks);
}

export function assignmentWindowReason(window: CoreBitBrowserWindow, tasks: CoreCollectionTask[], candidate?: CoreSplitCandidate): string {
  if (!window.locked) return '';
  const task = assignmentTaskScope(tasks, candidate).find(task => task.id === String(window.lock_entity_id || ''));
  if (window.lock_operation === 'collection' && task?.settings?.live_queue_enabled === true) return '';
  return assignmentSourceTask(tasks, candidate) ? '恢复目标只能由原任务的窗口继续；可取消已有选择' : '当前由其他任务占用；可取消已有选择';
}

function hasJoinedWindow(task: CoreCollectionTask, id: string, window?: CoreBitBrowserWindow): boolean {
  if (!task.windows?.some(member => member.profile_id === id)) return false;
  const profiles = task.runtime?.profile_states;
  const profile = Array.isArray(profiles) ? profiles.find(value => value && typeof value === 'object'
    && String(value.profile_id || value.window_id || '') === id) : undefined;
  // A drained window keeps its historical task membership after Core releases
  // its lease. It must explicitly join again. Current lease ownership wins over
  // a stale closed runtime entry; paused/failed windows remain joined.
  const closed = profile?.state === 'closed' && profile?.reason === 'collection_queue_drained';
  return !closed || Boolean(window?.locked && window.lock_operation === 'collection' && window.lock_entity_id === task.id);
}

export function assignmentHasJoinedWindow(tasks: CoreCollectionTask[], ids: string[], candidate?: CoreSplitCandidate) {
  return assignmentTaskScope(tasks, candidate).some(task => ids.some(id => hasJoinedWindow(task, id)));
}

// This plans future queue membership only. The backend still validates leases
// and atomically commits every actual join; this never opens or takes a page.
export function planAssignmentJoins(tasks: CoreCollectionTask[], windows: CoreBitBrowserWindow[], ids: string[], candidates: CoreSplitCandidate[] = []) {
  const joins = new Map<string, Set<string>>();
  const plannedOwner = new Map<string, string>();
  const eligible = collectionAssignmentTasks(tasks);
  const lookup = new Map(windows.map(window => [window.id, window]));
  const subjects: Array<CoreSplitCandidate | undefined> = candidates.length ? [...candidates] : [undefined];
  subjects.sort((a, b) => Number(Boolean(assignmentSourceTask(tasks, b))) - Number(Boolean(assignmentSourceTask(tasks, a))));
  for (const candidate of subjects) {
    const source = assignmentSourceTask(tasks, candidate);
    const scope = assignmentTaskScope(tasks, candidate);
    const sourceCanClaim = Boolean(source && ids.some(id => hasJoinedWindow(source, id, lookup.get(id))));
    for (const id of [...new Set(ids)]) {
      const window = lookup.get(id);
      // A pool is OR, not a requirement that every member join every source.
      // Batched recoveries may already each have a different valid member.
      if (sourceCanClaim && (!window || assignmentWindowReason(window, tasks, candidate))) continue;
      if (!window) throw new Error('指定窗口已不在列表中，请关闭弹窗后从等待列表移除失效窗口');
      const reason = assignmentWindowReason(window, tasks, candidate);
      if (reason) throw new Error(`${window.name}：${reason}`);
      if (scope.some(task => hasJoinedWindow(task, id, window))) continue;
      const planned = plannedOwner.get(id);
      if (planned && (!source || source.id === planned)) continue;
      if (source && sourceCanClaim && (planned || eligible.some(task => task.id !== source.id && hasJoinedWindow(task, id, window)))) continue;
      if (planned || (source && eligible.some(task => task.id !== source.id && hasJoinedWindow(task, id, window)))) {
        throw new Error(`${window.name} 无法同时加入不同恢复任务，请在等待列表分别指定窗口`);
      }
      const destination = source ? eligible.find(task => task.id === source.id) : eligible[0];
      if (!destination) {
        if (source) throw new Error('原采集任务当前不能加入窗口，请先恢复原任务，再重试窗口加入');
        continue; // No live task: keep the durable affinity for the next start.
      }
      const members = joins.get(destination.id) || new Set<string>();
      members.add(id); joins.set(destination.id, members); plannedOwner.set(id, destination.id);
    }
  }
  return [...joins].map(([taskId, values]) => ({ taskId, windowIds: [...values] }));
}
