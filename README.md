# IMG Link Migrator

**English** | [简体中文](README_CN.md)

A standalone desktop and command-line tool that migrates external images in
Markdown files to [ImgBB](https://imgbb.com/) and updates the original image
links.

It is designed for Obsidian vaults and works with any UTF-8 Markdown file or
folder. ImgBB is the first and currently the only supported image-hosting
provider.

## Features

- Select one Markdown file or a whole folder from a native GUI.
- Scan safely before making changes.
- Migrate all external image hosts, or restrict migration to selected domains
  such as `xhscdn.com`.
- Recognize inline Markdown images, HTML `<img>` elements, and Markdown
  reference-style images.
- Skip YAML frontmatter, fenced code blocks, inline code, hidden folders, and
  existing ImgBB links.
- Download each source image first, validate that it is an image, and enforce
  ImgBB's 32 MB limit.
- Upload with ImgBB API v1 using `multipart/form-data`.
- Show per-image progress, success, cache reuse, and failure details.
- Retry downloads and uploads automatically with exponential backoff.
- Retry failed items manually from the GUI.
- Deduplicate repeated URLs and identical image content.
- Atomically replace each completed image URL before processing the next image.
- Replace only successfully uploaded links; failed links remain unchanged.
- Detect files edited after scanning and avoid overwriting those changes.
- Use atomic file writes.
- Optionally back up every changed Markdown file before replacement.
- Cancel a running migration.
- Keep the API key out of files, reports, backups, and the persistent cache.

## Requirements

- Python 3.9 or newer
- Tk support for the GUI
- An ImgBB API key

Homebrew distributes Tk separately from Python. If the GUI reports that no
working Tk installation is available, find and install the formula matching
the selected Python interpreter:

```bash
brew search python-tk
```

Tk is only used by the GUI.

## Quick start

### GUI

On macOS, double-click `launch-gui.command`, or run:

```bash
python3 img_link_migrator.py --gui
```

Then:

1. Choose a Markdown file or vault folder.
2. Paste the ImgBB API key. It is kept in memory for the current run only.
3. Optionally enter one or more source domains, separated by commas.
4. Choose whether changed files should be backed up.
5. Click **Scan** to preview the detected links.
6. Click **Start migration** to upload and replace successful links.
7. If any item fails, click **Retry failed**.

The domain field can be left empty to migrate images from every external host.
ImgBB's own domains are always excluded.

### CLI

CLI commands must be run from the project directory, where
`img_link_migrator.py` is located.

#### Where to enter the API key

- **GUI:** paste it into the **ImgBB API key** field.
- **CLI (recommended):** create a temporary variable named `IMGBB_API_KEY` in
  the current terminal and use the API key as its value.
- **CLI (not recommended):** pass `--api-key "YOUR_KEY"` directly. The value
  may be recorded in shell history or process listings.

`IMGBB_API_KEY` is the fixed variable name and should not be changed. The
command used to set it depends on the current shell: fish, zsh, and bash use
different `read` syntax. Run `echo $SHELL`, then use only the matching group
below.

##### fish

A prompt resembling `directory (main)>`, together with an error mentioning
`See help identifiers`, usually indicates fish.

1. Copy the ImgBB API key.
2. Run the input command below.
3. When Terminal displays `API key:`, paste the key and press Return. fish may
   display `*` characters to mask the input.

```fish
read --silent --global --export --prompt-str 'API key: ' IMGBB_API_KEY
```

Confirm that it is set without displaying its value:

```fish
set --query IMGBB_API_KEY; and echo 'API key is set'; or echo 'API key is not set'
```

Clear it after use:

```fish
set --erase --global IMGBB_API_KEY
```

##### zsh

```zsh
read -s "IMGBB_API_KEY?API key: "; echo
export IMGBB_API_KEY
```

Confirm that it is set:

```zsh
if [[ -n "$IMGBB_API_KEY" ]]; then
  echo "API key is set"
else
  echo "API key is not set"
fi
```

Clear it after use:

```zsh
unset IMGBB_API_KEY
```

##### bash

```bash
IFS= read -r -s -p "API key: " IMGBB_API_KEY; echo
export IMGBB_API_KEY
```

Confirm that it is set:

```bash
if [[ -n "$IMGBB_API_KEY" ]]; then
  echo "API key is set"
else
  echo "API key is not set"
fi
```

Clear it after use:

```bash
unset IMGBB_API_KEY
```

Migration commands in this terminal session now read the key automatically.
The variable expires when the terminal session closes.

The key is needed only for `--apply`. Scanning and opening the GUI do not
require it to be set in advance.

To put the API key directly in the migration command, use the following full
form and replace `YOUR_IMGBB_API_KEY` with the actual key:

```bash
python3 img_link_migrator.py \
  --apply \
  --api-key "YOUR_IMGBB_API_KEY" \
  "$HOME/Documents/MyVault"
```

#### What the target path means

The target is the Markdown file or directory to scan:

- `./note.md` means `note.md` in the current directory.
- `./notes` means a child directory named `notes`. It is only an example.
- `"$HOME/Documents/MyVault"` is an example vault under the current user's
  documents directory.

Quote paths containing spaces or non-ASCII characters. On macOS, a file or
folder can be dragged from Finder into Terminal to insert its real path.

#### Common workflow

Replace `"$HOME/Documents/MyVault"` below with the actual file or directory.

First, scan without downloading, uploading, or changing files:

```bash
python3 img_link_migrator.py "$HOME/Documents/MyVault"
```

Then apply the migration after checking the scan output:

```bash
python3 img_link_migrator.py --apply "$HOME/Documents/MyVault"
```

To filter by source domain, add `--include-host`. The Xiaohongshu CDN below is
only an example; any other image domain can be used:

```bash
python3 img_link_migrator.py \
  --apply \
  --include-host xhscdn.com \
  "$HOME/Documents/MyVault"
```

Run without backups:

```bash
python3 img_link_migrator.py --apply --no-backup "$HOME/Documents/MyVault"
```

Create a machine-readable result report:

```bash
python3 img_link_migrator.py \
  --apply \
  --report report.json \
  "$HOME/Documents/MyVault"
```

### CLI modes and arguments

The three command forms are:

```text
GUI:           python3 img_link_migrator.py --gui
Scan example:  python3 img_link_migrator.py "$HOME/Documents/MyVault"
Apply example: python3 img_link_migrator.py --apply "$HOME/Documents/MyVault"
```

Notation used in command references:

| Notation | Meaning |
|---|---|
| `[value]` | Optional. Do not type the square brackets. |
| `<value>` | Replace it with a real value. Do not type the angle brackets. |
| `...` | The previous value can be repeated. Do not type the dots. |
| Uppercase word | Replace it with a value, such as a key, number, domain, or path. |

| Mode | Network requests | File changes | Use case |
|---|---:|---:|---|
| A target without `--apply` | No | No | Scan and print detected external image links. |
| `--apply` | Yes | Yes, for successful uploads | Run the migration from the terminal. |
| `--gui` | Only after starting a migration | Only after confirmation | Open the desktop interface and choose the target there. |

`--gui` and `--apply` are mutually exclusive.

“Default” describes what happens when an argument is omitted. A “flag” takes
no value: writing the flag enables its behavior.

| Argument | What to enter | When omitted | Purpose and example |
|---|---|---|---|
| Target path | One or more Markdown files or directories; do not type the word `targets`. | Scan/apply requires a target; running with no arguments opens the GUI. | `"$HOME/Documents/MyVault"` or `"note.md"` |
| `--gui` | Flag; enter no value after it. | Use CLI behavior. | `python3 img_link_migrator.py --gui` |
| `--apply` | Flag; enter no value after it. | Scan only. | `python3 img_link_migrator.py --apply "$HOME/Documents/MyVault"` |
| `--api-key KEY` | Replace `KEY` with the ImgBB API key. | Read `IMGBB_API_KEY`; apply mode fails if both are missing. | `--api-key "YOUR_IMGBB_API_KEY"`; the environment variable is safer. |
| `--expiration SECONDS` | Replace `SECONDS` with a number of seconds. | Default `0`, requesting permanent storage. | Temporary values: `60`–`15552000`; example: `--expiration 600`. |
| `--retries N` | Replace `N` with the retry count. | Default `3`, meaning up to three retries after the first failure. | Range `0`–`10`; `--retries 0` makes one attempt. |
| `--include-host DOMAIN` | Replace `DOMAIN` with an allowed source domain. | Process every detected external image host. | `--include-host xhscdn.com`; repeat or comma-separate values. |
| `--exclude-host DOMAIN` | Replace `DOMAIN` with an excluded source domain. | Add no extra exclusions; ImgBB hosts remain excluded. | `--exclude-host example.com`; repeat or comma-separate values. |
| `--state-dir PATH` | Replace `PATH` with the cache/backup directory. | Use the system app-data directory. | `--state-dir "./migrator-state"` |
| `--no-backup` | Flag; enter no value after it. | Back up Markdown before changes. | `--apply --no-backup "$HOME/Documents/MyVault"` |
| `--report PATH` | Replace `PATH` with a JSON report location. | Write no JSON report. | `--report "./report.json"` |
| `-h`, `--help` | Flag; enter no value after it. | Run normally. | `python3 img_link_migrator.py --help` |

## Backups and state

Backups are enabled by default and can be disabled:

- GUI: clear **Back up before changes**.
- CLI: add `--no-backup`.

The app stores its URL/content cache and optional backups outside the selected
vault:

- macOS: `~/Library/Application Support/IMG Link Migrator/`
- Windows: `%APPDATA%/IMG Link Migrator/`
- Linux: `$XDG_STATE_HOME/img-link-migrator/` or
  `~/.local/state/img-link-migrator/`

Each backup run has a timestamped directory and a `manifest.json` that maps
every original absolute path to its backup copy. Restoring a file only requires
copying that backup over the corresponding original path.

The persistent cache contains source URLs, SHA-256 image hashes, destination
URLs, and expiration timestamps. It never contains the ImgBB API key.

## Supported Markdown

```markdown
![alt text](https://example.com/image.png)

<img src="https://example.com/image.jpg" alt="Example">

![alt text][image-id]
[image-id]: https://example.com/image.webp
```

Normal links such as `[website](https://example.com/)` are intentionally not
treated as images.

## Safety model

The migration pipeline is:

1. Scan Markdown and collect replaceable image URLs.
2. Download and validate the first unique source image.
3. Reuse a valid cached ImgBB link when possible; otherwise upload the image.
4. As soon as that image is ready, process every Markdown file referencing it:
   - If backups are enabled and the file has not yet been backed up in this
     run, save one copy of its original state.
   - Verify that no other process has changed the file since the scan or the
     previous program write.
   - Atomically replace that image URL immediately.
5. Start downloading and uploading the next image only after the Markdown
   write has finished.
6. Repeat until the task completes or is cancelled.

Each file is backed up at most once per run, before its first migrated link is
written. A normal cancellation keeps all completed image replacements on disk
and leaves unprocessed links unchanged.

There is still a very short execution interval between receiving the upload
response and completing the local atomic write. If the process is forcibly
terminated during that interval, ImgBB may temporarily contain an image not
yet referenced by Markdown. The next run reuses the local cache and completes
the replacement.

## ImgBB expiration

The default expiration is `0`, which requests permanent storage. To request
automatic deletion, use a value from 60 through 15,552,000 seconds:

```bash
python3 img_link_migrator.py \
  --apply \
  --expiration 600 \
  "$HOME/Documents/MyVault"
```

Expired cache entries are not reused.

## Development

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

Run a syntax check:

```bash
python3 -m py_compile img_link_migrator.py
```

The tests use a fake ImgBB client and do not perform network requests.

## Current limitations

- Only UTF-8 Markdown files are modified.
- Source images that require an authenticated browser session may fail to
  download. Xiaohongshu CDN requests automatically include the Xiaohongshu
  website as the HTTP referrer, which is sufficient for many public links.
- URL parsing focuses on common Obsidian and Markdown image syntax. Links
  generated by custom plugins with nonstandard syntax may not be detected.
- The tool does not delete images from ImgBB.

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE).
