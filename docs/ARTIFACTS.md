# Reviewable Git Artifacts

Every delegated code run starts from a local Git checkpoint on a `lightclaw/<run-id>` branch inside its owned task workspace. When the run finishes, LightClaw stages the workspace delta and writes two owner-only review files beside the private receipt:

- `changes.patch`: a standard binary-safe Git patch;
- `artifact.json`: the base commit, branch, changed paths, diff stat, and patch SHA-256.

A task directory inside another Git repository gets its own repository before checkpoint staging. Every artifact Git command explicitly uses the opened directory's own `.git` and work tree, including linked worktrees. If the task repository is missing, review, acceptance, rejection, and publication fail rather than operating on a parent repository. These operations retain the requested directory path for the no-follow opener, so a root symlink cannot redirect them to another checkout.

Before each command, task Git metadata is checked through the opened workspace. A `.git` symlink
or special file, a gitfile pointing at a main repository or another worktree, and a `commondir`
redirect inside a task's own `.git` directory are refused. Linked worktrees retain Git's
[back-reference to their own `.git` file](https://git-scm.com/docs/gitrepository-layout);
the referenced workspace directory must match the opened task directory. Absolute and relative
worktree pointers are supported. The explicitly selected source repository for
`create_isolated_worktree` remains trusted, including repositories with separate Git metadata.
Task artifact commands disable Git hooks using a per-command
[`core.hooksPath` override](https://git-scm.com/docs/git-config), so checkpointing, acceptance,
and publication do not execute scripts supplied in task metadata or inherited hook paths.
Hook files and repository configuration are preserved. The explicitly selected source repository
keeps its normal hooks when creating an optional worktree. These controls cannot prevent another
host process from rewriting metadata after the final check and do not sandbox configured filters.

Task commands also override `core.fsmonitor` with an empty value, so configured monitor scripts
and built-in monitor daemons do not run during artifact handling. Git scans the actual files even
if a previously active monitor left cached index entries. An empty value also disables the
[legacy pathname-based setting](https://github.com/git/git/blob/v2.35.1/config.c), avoiding older
Git versions interpreting `false` as an executable pathname. Stored monitor configuration is
preserved; the explicitly selected source repository keeps its existing monitor behavior.

LightClaw-generated checkpoint and acceptance commits are unsigned. A per-command
[`commit.gpgSign=false` override](https://git-scm.com/docs/git-config) prevents inherited signing
settings from launching a signer, requesting host keys or passphrases, or failing an automatic
local commit. Signing configuration is preserved for deliberate signing with the user's Git CLI
after review; existing signed commits are not rewritten by acceptance.

Artifact comparisons disable external diff drivers and text conversion with
[`--no-ext-diff` and `--no-textconv`](https://git-scm.com/docs/git-diff). Configured helpers
cannot run during change detection, statistics, or patch generation, nor hide raw changes by
converting different files into identical text. Binary patches retain the actual staged bytes
and can be applied with Git; stored driver settings remain available for manual review.
Generated patches always use the standard `a/` and `b/` path prefixes, and comparisons suppress
terminal color codes. User settings such as `diff.noprefix`, mnemonic or custom prefixes, and
forced color cannot break patch application or add ANSI escapes to Telegram summaries and receipts.

Review artifacts support up to 500 changed paths. Larger runs fail review-artifact generation so the manifest never silently omits changed files; reduce the run scope before retrying.

Neither finishing a run nor generating these files contacts a remote. Accepting a result creates only a local commit.

Git artifact operations enter an opened workspace directory before running Git. A workspace replaced by a symlink before opening is refused; replacing its path after opening cannot redirect staging, commits, resets, or patch inspection into the replacement repository.

## Review a result

```bash
lightclaw artifact status <run-id>
lightclaw artifact accept <run-id>
lightclaw artifact reject <run-id>
```

Commands that change state are previews by default. Re-run the exact reviewed action with `--apply`:

```bash
lightclaw artifact accept <run-id> --apply
lightclaw artifact reject <run-id> --apply
```

`accept` requires a successfully completed durable run and commits the staged result on its local LightClaw branch. `reject` only unstages the result and preserves every workspace file for inspection. Neither action pushes.

## Apply selected files

Preview exact regular files before copying them into another local checkout:

```bash
lightclaw artifact apply <run-id> \
  --target /path/to/your/repository \
  --paths service.py tests/test_service.py
```

Review the JSON plan and copy its `plan_sha256` value into the apply command:

```bash
lightclaw artifact apply <run-id> \
  --target /path/to/your/repository \
  --paths service.py tests/test_service.py \
  --apply \
  --confirm-plan <plan_sha256>
```

LightClaw rejects absolute paths, traversal, symlink sources, and symlinks anywhere in selected target
or backup paths. Source and target root paths remain visible to the no-follow opener even if a
directory is replaced after initial validation. During `--apply`, it opens selected files without
following symlinks, copies relative
to opened workspace directories, and requires the confirmed plan hash to match a fresh preview of
the source and target hashes, paths, permissions, and operations. A stale plan is refused. A source
change while staging, or a target change while creating its backup, aborts before any selected file
is replaced. All selected sources are staged and verified before target backups begin. New files
are created without overwriting a target that appeared concurrently. Existing selected files are
copied to private `.lightclaw-backups/<run-id>/` directories first; an existing
backup is never overwritten. Failed backup preparation removes backups created in that attempt;
cleanup failures are reported. Unselected and unrelated files are never touched.

Immediately before replacing an existing file, LightClaw rechecks its content, permissions, and
identity through the opened target directory. A change since backup preparation refuses that
replacement and retains the backup. Copying is atomic per file, not a transaction across the whole
selection; pause competing writers when applying, since a write after the final check can still
race replacement.

## Optional pull request

A PR preview reads the private receipt and includes the approved goal/scope, risk and capability, diff summary, and actual check evidence. The artifact must first be accepted into a clean local commit; uncommitted or unchanged branches remain preview-only:

```bash
lightclaw artifact pr <run-id> --title "Add service health check"
```

Publishing requires all of the following: an `origin` remote, an authenticated `gh` CLI, `--apply`, and an exact run-ID confirmation:

```bash
lightclaw artifact pr <run-id> \
  --title "Add service health check" \
  --apply \
  --confirm-publish <run-id>
```

Only that final command pushes the isolated branch and opens a pull request. There is no implicit push, PR, release, package publication, or other external write.

PR creation uses an opened repository directory and an owner-only temporary body file outside the repository. Its body file is removed after success, failure, or timeout; cleanup never unlinks a path in a replaced workspace. A workspace symlink present before publication is refused before Git push.

## Deterministic proof

`lightclaw demo --scenario repo-task` replays a recorded phone/Telegram request and approval, creates a baseline repository, applies a bounded health-check change, runs a real unit test, and returns `phone-to-patch.json`, a valid private receipt, `review/changes.patch`, and `review/artifact.json`. It needs no Telegram account, model, token, network, or hidden manual step.

The demo proves the local request-to-verified-patch loop. It does not prove that an external coding model will produce a correct change or that GitHub authentication is configured.
