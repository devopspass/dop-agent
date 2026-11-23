# Session groups

## Session groups

Sessions can be organised into **groups** (workstreams): a session is in at most
one group, groups don't nest, and "no group" is the default (shown as *Ungrouped*).
One member may be the group's **master** (always listed first). Archiving a group
archives its sessions; deleting a group only ungroups them.

- **Web UI:** group chips above the session list filter it (All / Ungrouped / each
  group, with a status dot: 🔴 failed, 🟡 running, 🔵 unread, ⚪ idle). `＋` creates a
  group, `☰ Grouped` toggles collapsible group headers. Right-click a session →
  *Move to group…* (searchable, new group, remove, make master), or drag a session
  onto a chip/header. Opening a grouped session shows a banner with prev/next sibling
  navigation; ungrouped sessions may show a dismissible "looks related to group X"
  suggestion. 📊 (header) / double-click a chip opens the **group overview**: needs
  attention, sessions table, linked MRs/Jira tickets (extracted from messages), active
  watches, timeline. In the grouped view, drag a session onto a sibling (upper/lower half) to reorder it within its group (master stays first; saved as `group_order`, `PUT /api/groups/member-order`). Drag a group *header* onto another header to reorder the groups themselves (`sort_order`, `PUT /api/groups/order`).
- **MCP tools:** `group_list` + `group_manage(action=create|update|assign|set_master|archive|delete)`,
  `session_list(group_id=...)` (`"none"` = ungrouped), and
  `session_create(parent_session_id=..., inherit_group=True)` (or `group_id=...`) —
  children inherit the parent's group **by default** (`inherit_group=False` opts out); a parent without a group gets
  one auto-created from its title and becomes master.
- **REST:** `/api/groups` (GET/POST), `/api/groups/{id}` (PATCH/DELETE), `.../archive`,
  `.../master`, `.../overview`, `/api/groups/suggest`, `PUT /api/sessions/{id}/group`;
  `/api/sessions` and the SSE stream also return `groups`.
- **Backfill:** `python3.13 group_backfill.py` (dry run) / `--apply` (backs up the DB first).
