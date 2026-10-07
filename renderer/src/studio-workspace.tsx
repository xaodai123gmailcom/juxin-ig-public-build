import {StandaloneNurtureWorkspace} from './standalone-nurture-workspace';
import type {CoreWorkbenchSnapshot} from './core-client';

/** The studio entry now contains only account nurture. */
export function StudioWorkspace(props: {mode: 'nurture'; snapshot: CoreWorkbenchSnapshot; refreshWindows?: () => Promise<unknown>}) {
  return <StandaloneNurtureWorkspace snapshot={props.snapshot} refreshWindows={props.refreshWindows}/>;
}
