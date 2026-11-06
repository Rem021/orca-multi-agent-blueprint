import { adapter } from './adapter.mjs';
import { planNames } from './core.mjs';
const s = await adapter.snapshot();
console.log(JSON.stringify({ workspaces: s.worktrees.length, pending: planNames(s.worktrees, s.repos, s.snapshots) }, null, 2));
