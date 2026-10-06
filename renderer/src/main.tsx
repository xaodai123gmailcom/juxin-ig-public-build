import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { RenderBoundary } from "./render-boundary";
import "./global.css";
import "./workbench-polish-r55.css";
import "./workbench-density.css";
import "./workbench-r93.css";

createRoot(document.getElementById("root")!).render(<React.StrictMode><RenderBoundary><App /></RenderBoundary></React.StrictMode>);
