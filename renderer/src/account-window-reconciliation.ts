export type AccountWindowReconciliationLock = {
  operation_type: string;
  state?: string;
  entity_id?: string | null;
  can_reconcile_window_state?: boolean;
};

/** Eligibility is an explicit server decision, never guessed from a busy window. */
export function accountWindowReconciliationEligible(lock?: AccountWindowReconciliationLock) {
  return lock?.can_reconcile_window_state === true
    && lock.operation_type === 'account' && lock.state === 'occupied' && lock.entity_id === null;
}

export function accountWindowReconciliationReady(lock: AccountWindowReconciliationLock | undefined, window: {opened?: boolean; window_state?: string | null} | undefined, stale = false) {
  return accountWindowReconciliationEligible(lock) && !stale
    && window?.opened === false && window.window_state !== 'unknown';
}
