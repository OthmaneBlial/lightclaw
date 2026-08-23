# Install, Upgrade, and Uninstall

LightClaw requires Python 3.10–3.13 on macOS or Linux. The package distribution is named `lightclaw-ai`; the installed command is `lightclaw`.

## Install from the repository

For a standard isolated tool installation:

```bash
pipx install 'git+https://github.com/OthmaneBlial/lightclaw.git'
# or
uv tool install 'git+https://github.com/OthmaneBlial/lightclaw.git'
```

For a local checkout:

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git
cd lightclaw
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
lightclaw --help
```

The compatibility installer also creates an isolated virtual environment and never installs into global Python:

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git
cd lightclaw
bash setup.sh
```

Do not use `curl | sh` for privileged tooling unless you first download, inspect, and pin the script to a trusted commit.

## First run and paths

```bash
lightclaw onboard --configure
lightclaw run
```

Default paths:

| Purpose | Path |
|---|---|
| Private configuration | `~/.config/lightclaw/config.env` |
| Runtime, memory, skills | `~/.lightclaw/` |
| Task workspaces | `~/.lightclaw/workspace/` |
| Compatibility installer | `~/.local/share/lightclaw/` |
| Command symlink | `~/.local/bin/lightclaw` |

Configuration files are written with mode `0600`. An existing config is kept unless reset is explicitly requested; reset creates a timestamped private backup first. A legacy `~/.env` can be copied once for migration and is never deleted automatically.

At least one numeric `TELEGRAM_ALLOWED_USERS` ID is required. Intentionally public bots require `LIGHTCLAW_PUBLIC_BOT_ACK=yes` and should still be isolated from sensitive host data.

## Optional container

Build the pinned Python 3.13 image locally:

```bash
docker build -t lightclaw:local .
docker run --rm lightclaw:local --help
```

Mount app config read-only and runtime data read/write when running the bot. The image uses an unprivileged `lightclaw` user. Container isolation is an additional boundary, not permission to expose a public high-authority bot.

## Upgrade

With pipx:

```bash
pipx upgrade lightclaw-ai
```

With uv:

```bash
uv tool upgrade lightclaw-ai
```

For a Git installation, repeat the install command with the desired release tag. Review release notes and back up `~/.config/lightclaw/` and `~/.lightclaw/` before crossing a documented migration boundary.

## Undo a delegated task

Run results display an exact task workspace label. Preview removal first:

```bash
lightclaw undo 20260823_120000_example-task
lightclaw undo 20260823_120000_example-task --apply
```

Undo works only for direct child workspaces with a matching external LightClaw ownership record. It refuses arbitrary paths, traversal, symlinks, and unregistered user directories.

## Uninstall

Preview the managed compatibility-install targets:

```bash
lightclaw uninstall --dry-run
lightclaw uninstall --apply
```

Configuration and runtime data are preserved by default. To remove them too:

```bash
lightclaw uninstall --apply --purge-data --yes
```

For pipx or uv installations, use that tool's uninstall command. Removing the package does not delete `~/.config/lightclaw/` or `~/.lightclaw/`; delete those only after reviewing and backing up anything you need.
