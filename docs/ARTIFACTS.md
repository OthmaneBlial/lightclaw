# Reviewable Git Artifacts

Every delegated code run starts from a local Git checkpoint on a `lightclaw/<run-id>` branch inside its owned task workspace. When the run finishes, LightClaw stages the workspace delta and writes two owner-only review files beside the private receipt:

- `changes.patch`: a standard binary-safe Git patch;
- `artifact.json`: the base commit, branch, changed paths, diff stat, and patch SHA-256.

Neither finishing a run nor generating these files contacts a remote. Accepting a result creates only a local commit.

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

Apply only after reviewing the JSON plan:

```bash
lightclaw artifact apply <run-id> \
  --target /path/to/your/repository \
  --paths service.py tests/test_service.py \
  --apply
```

LightClaw rejects absolute paths, traversal, symlink sources, and symlink destinations. Existing selected files are copied to `.lightclaw-backups/<run-id>/` first. Unselected and unrelated files are never touched.

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

## Deterministic proof

`lightclaw demo --scenario repo-task` replays a recorded phone/Telegram request and approval, creates a baseline repository, applies a bounded health-check change, runs a real unit test, and returns `phone-to-patch.json`, a valid private receipt, `review/changes.patch`, and `review/artifact.json`. It needs no Telegram account, model, token, network, or hidden manual step.

The demo proves the local request-to-verified-patch loop. It does not prove that an external coding model will produce a correct change or that GitHub authentication is configured.
