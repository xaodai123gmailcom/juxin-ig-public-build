import { useEffect, useState } from "react";
import { AuthGate } from "./auth-gate";
import { FormalWorkbench, type FormalWorkbenchMode } from "./formal-workbench";

export function modeFromHash(hash: string): FormalWorkbenchMode {
  const route = hash.replace(/^#/, "").replace(/\/$/, "") || "/";
  if (route === "/" || route === "/home") return "home";
  if (route === "/collection") return "collection";
  if (route === "/reports") return "reports";
  if (route === "/accounts") return "accounts";
  if (route === "/review") return "review";
  if (route === "/public") return "public";
  if (route === "/private") return "private";
  if (route === "/follow-monitor") return "follow-monitor";
  if (route === "/history") return "history";
  if (route === "/nurture") return "nurture";
  if (route === "/data") return "reports";
  if (route === "/settings") return "settings";
  return "home";
}

export default function App() {
  const [mode, setMode] = useState<FormalWorkbenchMode>(() => modeFromHash(window.location.hash));

  useEffect(() => {
    const update = () => {
      // Old bookmarks land on the single merged page without creating a second
      // history entry or reviving the retired standalone Data workspace.
      if (window.location.hash.replace(/\/$/, "") === "#/data") {
        window.history.replaceState(window.history.state, "", "#/reports");
      }
      setMode(modeFromHash(window.location.hash));
    };
    update();
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);

  return <AuthGate><FormalWorkbench mode={mode} /></AuthGate>;
}
