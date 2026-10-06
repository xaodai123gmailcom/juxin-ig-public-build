import { Component, type ErrorInfo, type ReactNode } from "react";

export class RenderBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() { return { failed: true }; }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("采集器界面渲染异常", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <main role="alert" style={{ minHeight: "100vh", display: "grid", placeContent: "center", gap: 16, padding: 32, background: "#020d1c", color: "#e8eef8" }}>
      <h1 style={{ fontSize: 24 }}>界面遇到异常</h1>
      <p>请重新加载界面查看任务状态。已保存的采集记录和进度会保留。</p>
      <button type="button" onClick={() => window.location.reload()} style={{ padding: "12px 24px", fontSize: 16, cursor: "pointer" }}>重新加载界面</button>
    </main>;
  }
}
