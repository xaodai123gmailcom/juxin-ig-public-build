import { createContext, useContext, useEffect, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { getCollectorCoreClient } from "./core-client";

type DraftContext = [string, Dispatch<SetStateAction<string>>];
const Context = createContext<DraftContext | null>(null);
const INSTAGRAM = { platform: "instagram" as const };

/** Keep the collection draft across route navigation without platform switching. */
export function WorkbenchPlatformProvider({ children }: { children: ReactNode }) {
  const draft = useState("");
  useEffect(() => {
    // This retired key held only a display choice, never account/session data.
    try { window.localStorage.removeItem("juxin.workbench.display-platform.v1"); } catch { /* Preference storage may be unavailable. */ }
  }, []);
  return <Context.Provider value={draft}>{children}</Context.Provider>;
}

export function useWorkbenchPlatform() { return INSTAGRAM; }

export function usePlatformCore() {
  return useMemo(() => getCollectorCoreClient(undefined, "instagram"), []);
}

export function useCollectionDraft(): DraftContext {
  const value = useContext(Context);
  if (!value) throw new Error("Workbench draft provider is missing");
  return value;
}
