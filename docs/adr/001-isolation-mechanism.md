# ADR 001: User & Workspace Isolation via Scoped Queries + Git Worktrees

## Status
Accepted (Phase 1)

## Context
The assignment's hard requirement is isolation "by construction, enforced
server-side" (§2.4, §3) — not a filter that a future PR can accidentally
remove, but something structural. Two separate isolation problems exist:

1. **Data isolation**: User A must never read or write User B's workspaces,
   tasks, or run artifacts via the API.
2. **Filesystem isolation**: Two tasks running concurrently against the
   *same* workspace must not see each other's uncommitted edits or corrupt
   each other's working tree.

## Decision

### Data isolation: single scoped-fetch function per resource
Every workspace or task lookup, anywhere in the codebase, goes through
exactly one function (`get_owned_workspace`, `get_owned_task`) whose query
includes `WHERE id = ? AND user_id = ?` in the same statement — never a
fetch-then-filter. There is no second code path (admin override, internal
tool, etc.) that fetches by id alone. This makes the isolation boundary a
property of the data-access layer, not of route-handler discipline: a
future route can forget to check ownership, but it cannot forget to call
the only function available for the lookup.

We return **404, not 403**, for "doesn't exist" and "exists but isn't
yours" alike. This is a real tradeoff: 403 is more informative to a
legitimate caller who mistyped an id, but it also confirms the id exists,
which lets a caller enumerate valid ids belonging to other users by
bisecting status codes. We chose to leak nothing over being more helpful;
revisit if support tooling needs to distinguish the two cases internally
(it can, via a separate internal-only query, without changing the public
contract).

The same pattern extends to `journal_events` and `budget_ledger`: both
carry `task_id`, and every read of them joins through a task already
fetched via `get_owned_task`, so there's no way to stream another user's
journal even if you know their task id.

### Filesystem isolation: one Git worktree + branch per task
Each workspace has exactly one canonical repo clone on disk
(`/data/repos/<workspace_id>`). Each task gets `git worktree add -b
task/<task_id>` off that repo's default branch — a real, independent
working directory that shares the `.git` object store but has its own
index and checked-out files.

Alternatives considered:
- **Full clone per task.** Correct, but expensive at the repo sizes the
  assignment specifies as the default case (doesn't fit in context ⇒
  likely doesn't fit in a fast per-task clone either). Worktrees give the
  same working-directory independence for the cost of a checkout.
- **Copy-on-write filesystem snapshots** (e.g. Btrfs/ZFS subvolumes,
  `cp --reflink`). Faster than worktrees on a COW-capable filesystem, but
  ties the design to host filesystem support and adds an OS-level
  dependency the grader's environment may not have. Worktrees work
  identically on any filesystem Git itself supports, which made them the
  right default for a take-home that has to run via `docker-compose up`
  on an unknown host.
- **One shared working directory with file locking.** Rejected outright —
  this is exactly the "two tasks corrupt each other's working tree"
  failure mode in §2.4 that worktrees exist to prevent.

Concurrent tasks on the same workspace therefore cannot collide on disk by
construction: they're on different branches in different directories. The
only point of contention is **merging a verified task branch back into the
default branch** (`mode=apply`), which is a deliberate, single choke point
(`merge_into_base` in `app/retrieval/workspace.py`) guarded by a
filesystem lock per workspace. A second task whose merge can't fast-forward
gets a `WorkspaceConflictError` with the conflicting file list — never a
silent overwrite, satisfying §2.4's "detect conflicts and serialize or
reject safely."

## Where this breaks down (org/tenant — design only, §2.6)
Extending to multi-tenant orgs means adding an `org_id` dimension above
`user_id`: workspaces would carry both an owning org and an owning user (or
team), and `get_owned_workspace` would need an org-membership check, not
just an equality check, since "my org's workspace, shared with teammates"
is no longer a single-owner relationship. The single-scoped-fetch-function
pattern still holds — it would just check membership instead of equality —
but the query gets a join against an `org_members` table, and the 404-vs-403
tradeoff gets sharper: within an org, a 403 ("you're in this org but not
this workspace's ACL") is much more useful feedback than a blanket 404, so
the right design likely distinguishes cross-org access (404) from
same-org-no-ACL access (403). We have not built this; it's flagged here
because it's the first place this Phase 1 decision would need to change
shape, not just scale.

## Consequences
- Every new resource type added later (e.g. a review/PR surface, §2.6) must
  add its own single scoped-fetch function following this pattern, or the
  isolation guarantee silently doesn't extend to it.
- The 404-not-403 choice means client-side debugging of "why can't I see
  workspace X" is slightly harder (looks identical to "X doesn't exist").
  We accept this for the security property.
