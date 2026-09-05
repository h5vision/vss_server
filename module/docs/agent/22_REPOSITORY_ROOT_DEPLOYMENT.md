# Repository Root Deployment

Deploy in this order: commit and push `module`, merge and push `test-merge`,
then pull `test-merge` on AWS and run SSH verification.

PR 9.2-A requires `SNAPSHOT_REPOSITORY_ROOT=/home/ubuntu/repos` in the
service environment and the same path in systemd `ReadWritePaths`.
Keep `SNAPSHOT_MATERIALIZATION_ROOT=/home/ubuntu/vss-snapshots` separate.
The preflight now checks that the repository root exists, is not a symlink,
and permits creation and rename as the service user inside systemd restrictions.

Before switching roots, stop the Backend and Admin services and copy the old
`.repository-cache` from the materialization root to the repository root.
Preserve the original cache. Do not overwrite an existing destination cache:
inspect conflicts first. Validate bare repositories and exact commit objects
before restarting. Metadata in PostgreSQL is retained; no DB migration is needed.

Validate the reported commit with `git cat-file -t` in the new bare cache,
then issue authenticated Admin Materialize for that exact repository/commit.
Materialize must not call VSS. Index is a separate explicit Admin action.
