import type { Rectangle } from 'electron';

/** A collector's hit-testing coordinates must not follow workspace navigation. */
export function accountViewport(task: boolean, active: boolean, current: Rectangle, workspace: Rectangle, posting?: Pick<Rectangle,'width'|'height'>): Rectangle {
  if (task) return {x: 0, y: 0, width: posting?.width ?? 1280, height: posting?.height ?? 900};
  if (!active) return current;
  return {x: 0, y: 0, width: workspace.width, height: workspace.height};
}
