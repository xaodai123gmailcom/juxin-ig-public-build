// One contract for both source preflight and final renderer output. Keeping
// separate copy lists let the r38 UI retire text still required after compiling.
export const requiredRendererReleaseCopy = Object.freeze([
  "仅清理预览缓存",
  "待审核预览", "Core 总数", "上次自动维护", "预览数据", "SQLite + WAL 占用",
  "全部不限", "直接丢弃规则", "启用直接丢弃",
  "公开帖子活跃度上限（天）", "全局去重",
  "请至少输入一条非空打招呼话术", "实际话术",
  "本次快照更新失败，正在自动重试", "总览数据暂未更新；独立读取正常的审核名单仍可操作，已有任务可暂停或停止",
  "立即重试",
]);

export const retiredPublishingMarkers = Object.freeze([
  'PostingWorkspace', 'posting-workspace', '/api/posting/', '/api/internal/integrations/pexels',
  'confirmed_posting', 'Pexels', 'PEXELS_API_KEY',
]);

export function verifyRendererReleaseCopy(text, label) {
  for (const marker of retiredPublishingMarkers) {
    if (text.includes(marker)) throw new Error(`NewGen desktop verification failed: ${label} contains retired publishing marker: ${marker}`);
  }
  for (const marker of ["部分列表显示最近", "前往设置", "界面 r94 / Core", "刷新页面"]) {
    if (text.includes(marker)) throw new Error(`NewGen desktop verification failed: ${label} contains retired shell marker: ${marker}`);
  }
  for (const marker of requiredRendererReleaseCopy) {
    if (!text.includes(marker)) {
      throw new Error(`NewGen desktop verification failed: ${label} is missing contract marker: ${marker}`);
    }
  }
}

export function verifyRendererProductionRuntime(text) {
  for (const marker of ['react_stack_bottom_frame', 'react.development.js',
    'react-dom-client.development.js', 'react.dev/link/react-devtools']) {
    if (text.includes(marker)) {
      throw new Error(`NewGen desktop verification failed: React development runtime found in release output: ${marker}`);
    }
  }
}
