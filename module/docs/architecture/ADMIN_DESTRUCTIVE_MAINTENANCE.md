# Admin destructive maintenance boundary

## Repository purge

`DELETE /v1/admin/repositories/{repository_id}/purge?confirm=<repository_id>` is an explicit admin-only hard-delete operation. It is separate from the existing Repository DELETE route, which remains a soft deactivate.

The purge removes module-owned Repository metadata in FK-safe order: Snapshot attempts/deltas, change-request revisions, branch/tag history, commit parent edges, Snapshots, bindings, tracked branches, sync runs, change requests, tags, commit catalog rows, and the Repository row itself. A running Repository sync, running commit catalog, or Snapshot in `submitting`, `accepted`, or `indexing` blocks the purge with `REPOSITORY_PURGE_BUSY`.

Before deleting, module collects the exact VSS project IDs referenced by the Repository. Those IDs are returned and audited, but VSS indexes are not implicitly deleted. Repository registration/history and vector indexes are independent destructive selections.

## VSS vector project deletion

`DELETE /v1/admin/vss/projects/{project_id}?confirm=<project_id>` is an admin-only orchestration endpoint. Browser and module never write pgvector/Chroma directly. The Backend calls the VSS-owned maintenance contract:

```text
DELETE /projects?project_id=<exact-project-id>
→ 204 No Content
```

After a 204 response module verifies `GET /index/exists?project_id=...` reports `exists=false` before recording success.

At `h5vision/vss_server` `test-merge` commit `401b7276e06b47ac78fe8fe3a92a51ec462e039b`, the VSS Store protocol already contains `drop(project_id)` and pgvector's implementation deletes project revisions/chunks through the VSS schema. However, that commit does **not** expose the destructive HTTP route yet. Module therefore fails closed with `VSS_PROJECT_DELETE_UNSUPPORTED` when the current VSS deployment returns 404; it does not bypass the VSS ownership boundary with direct SQL.

When VSS exposes this route, VSS should own all related cleanup, including Store data, BM25 final/staging files, and any in-memory job state for the exact project. Module should only request and verify deletion.

## Admin Web safeguards

Both permanent deletions require:

- admin role,
- CSRF/HMAC BFF allowlist checks,
- an exact confirmation value matching the selected UUID/project ID,
- an in-page confirmation dialog,
- an Audit Log entry on success.

Repository purge does not automatically delete vectors, and vector deletion does not alter Repository/Git history. This separation prevents a single mistaken click from destroying both provenance and retrieval state.
