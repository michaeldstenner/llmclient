# macOS Sequoia + Python 3.13: .pth files silently skipped

## Symptom

Entry point scripts fail with `ModuleNotFoundError: No module named 'llmclient'`
even though the editable install appears correct.  Running with `python3 -v`
shows:

```
Skipping hidden .pth file: '.../site-packages/llmclient-src.pth'
Skipping hidden .pth file: '.../site-packages/__editable__.llmclient-0.4.1.pth'
```

## Root cause

macOS Sequoia (15+) adds a `com.apple.provenance` extended attribute to
every file written on the system to track its origin application.  The
uv-managed Python 3.13 distribution treats any `.pth` file bearing this
xattr as "hidden" and skips it during site-packages initialization.

This effectively breaks all editable installs (`pip install -e` / `uv sync`
with editable packages) when the venv's Python is invoked directly via its
binary path (shebangs, symlinks, etc.).

The xattr **cannot be removed**: `xattr -d com.apple.provenance` exits 0
but the attribute is immediately re-added by the OS kernel.  New files
created in any terminal also receive the attribute.

## What doesn't work

- `python3 script.py` where script.py uses the venv's shebang
- Symlinks in `~/bin/` that point to `.venv/bin/<entry-point>`
- `uv run <entry-point>` — resolves to `.venv/bin/<script>` which runs
  under the same broken shebang

## What works

`uv run python3 -m <package>` — uv sets up the environment correctly,
bypassing the `.pth` mechanism entirely, before handing off to Python.

## Fix applied (llmclient)

1. Added `llmclient/cli/__main__.py` so the CLI is invocable as
   `python3 -m llmclient.cli`.

2. Added `bin/llmc` (committed, shell wrapper):
   ```sh
   #!/bin/zsh
   exec uv run --project "$(cd "$(dirname "$0")/.." && pwd)" \
       python3 -m llmclient.cli "$@"
   ```

3. The symlink at `~/bin/llmc` should point to `bin/llmc`, not to
   `.venv/bin/llmc`:
   ```sh
   ln -sf ~/Documents/Code/llmclient/bin/llmc ~/bin/llmc
   ```

## Pattern for other entry points

Any project with editable llmclient (or its own editable install) that
exposes a `~/bin/` command should follow the same pattern:

- Add `<package>/__main__.py`
- Add a `bin/<cmd>` shell wrapper in the project using `uv run ... python3 -m <package>`
- Symlink `~/bin/<cmd>` → `<project>/bin/<cmd>` (NOT → `.venv/bin/<cmd>`)

## Open questions

- Is this a uv-specific Python build or stock CPython 3.13 behavior?
- Will this be fixed upstream?  Worth filing against uv or cpython.
- Other tools (squirrel, watchdog, pithos) — do their entry points hit
  this same issue?  Check their `~/bin/` symlink targets.
