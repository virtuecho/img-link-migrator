# IMG Link Migrator

**English** | [简体中文](README_CN.md)

This repository contains two independent image-link migration programs:

1. A Python Markdown GUI/CLI in `img_link_migrator.py`.
2. A self-contained macOS program in `img-link-migrator.command` for raw image
   URLs in text and Markdown files.

Each program has its own runtime, scanning rules, prompts, cache, and safety
behavior. The documentation for one program does not apply to the other.
Both programs support these upload destinations:

- [ImgBB API v1](https://api.imgbb.com/1/upload)
- [PicGo.net API v1.1](https://www.picgo.net/api-v1/?lang=en), based on the
  [Chevereto API v1](https://v4-docs.chevereto.com/api/1/file-upload.html)

## Python Markdown GUI/CLI

The Python program is designed for Obsidian vaults and other UTF-8 Markdown
files. It understands common Markdown image syntax rather than treating every
URL as an image.

### Python upload providers

| `--provider` value | Destination | API key environment variable | API base URL |
| --- | --- | --- | --- |
| `imgbb` | ImgBB | `IMGBB_API_KEY` | Fixed ImgBB API v1 endpoint. |
| `chevereto` | PicGo.net or another Chevereto site | `CHEVERETO_API_KEY` | Defaults to `https://www.picgo.net`; the CLI can change it with `--chevereto-url`. |

Provider caches are isolated. Switching the destination never reuses a URL or
content-hash cache entry created for another provider.

The GUI provides a PicGo.net preset. In the Python CLI, selecting `chevereto`
uses the `X-API-Key` header and uploads the image in the `source` multipart
field to `/api/1/upload`. A custom Chevereto site selected with
`--chevereto-url` uses the same standard endpoint on that site.

### Python features

- Select one Markdown file or a whole folder from a native GUI.
- Scan safely before making changes.
- Migrate every external image host or restrict migration to selected domains.
- Recognize inline Markdown images, HTML `<img>` elements, and Markdown
  reference-style images.
- Skip YAML frontmatter, fenced code blocks, inline code, hidden folders, and
  existing links from the selected destination.
- Download and validate each source image and enforce a 32 MB local safety
  limit.
- Show per-image progress, cache reuse, success, and failure details.
- Retry automatically, with manual retry available in the GUI.
- Deduplicate repeated URLs and identical image content.
- Atomically replace each completed image URL before processing the next image.
- Optionally back up changed Markdown files.
- Detect files edited after scanning and avoid overwriting those changes.
- Keep the API key out of files, reports, backups, and the persistent cache.

### Python requirements

- Python 3.9 or newer
- An API key for the selected upload provider
- Tk for the GUI

Homebrew distributes Tk separately from Python. If the GUI reports that no
working Tk installation is available, find and install the formula matching
the selected Python interpreter:

```bash
brew search python-tk
```

Tk is used only by the GUI.

### Python GUI

On macOS, double-click `launch-gui.command`, or run:

```bash
python3 img_link_migrator.py --gui
```

Then:

1. Choose a Markdown file or vault folder.
2. Select **ImgBB** or **PicGo.net (Chevereto)** as the upload provider.
3. Paste that provider's API key. It is kept in memory for the current run.
4. Optionally enter one or more source domains, separated by commas.
5. Choose whether changed files should be backed up.
6. Click **Scan** to preview the detected links.
7. Click **Start migration** to upload and replace successful links.
8. If any item fails, click **Retry failed**.

The domain field can be left empty to migrate images from every external host.
Domains belonging to the selected destination are excluded.

### Python CLI

CLI commands must be run from the project directory, where
`img_link_migrator.py` is located.

#### Where to enter the API key

- **GUI:** select the provider, then paste its key into the API-key field.
- **CLI:** use the environment variable matching `--provider`.

| Provider | CLI selector | Environment variable |
| --- | --- | --- |
| ImgBB | `--provider imgbb` or omit `--provider` | `IMGBB_API_KEY` |
| PicGo.net / Chevereto | `--provider chevereto` | `CHEVERETO_API_KEY` |

The command used to set a variable depends on the current shell. Run
`echo $SHELL`, then use only the matching group below and only the line for the
selected provider.

##### fish

A prompt resembling `directory (main)>`, together with an error mentioning
`See help identifiers`, usually indicates fish.

1. Copy the selected provider's API key.
2. Run the input command below.
3. When Terminal displays `API key:`, paste the key and press Return. fish may
   display `*` characters to mask the input.

```fish
# ImgBB
read --silent --global --export --prompt-str 'API key: ' IMGBB_API_KEY

# PicGo.net / Chevereto
read --silent --global --export --prompt-str 'API key: ' CHEVERETO_API_KEY
```

Confirm the selected variable without displaying its value:

```fish
set --query IMGBB_API_KEY; and echo 'API key is set'; or echo 'API key is not set'
set --query CHEVERETO_API_KEY; and echo 'API key is set'; or echo 'API key is not set'
```

Clear it after use:

```fish
set --erase --global IMGBB_API_KEY
set --erase --global CHEVERETO_API_KEY
```

##### zsh

```zsh
# ImgBB
read -s "IMGBB_API_KEY?API key: "; echo
export IMGBB_API_KEY

# PicGo.net / Chevereto
read -s "CHEVERETO_API_KEY?API key: "; echo
export CHEVERETO_API_KEY
```

Confirm that it is set:

```zsh
# Run only the line for the selected provider.
[[ -n "${IMGBB_API_KEY:-}" ]] && echo "API key is set" || echo "API key is not set"
[[ -n "${CHEVERETO_API_KEY:-}" ]] && echo "API key is set" || echo "API key is not set"
```

Clear it after use:

```zsh
unset IMGBB_API_KEY
unset CHEVERETO_API_KEY
```

##### bash

```bash
# ImgBB
IFS= read -r -s -p "API key: " IMGBB_API_KEY; echo
export IMGBB_API_KEY

# PicGo.net / Chevereto
IFS= read -r -s -p "API key: " CHEVERETO_API_KEY; echo
export CHEVERETO_API_KEY
```

Confirm that it is set:

```bash
# Run only the line for the selected provider.
[[ -n "${IMGBB_API_KEY:-}" ]] && echo "API key is set" || echo "API key is not set"
[[ -n "${CHEVERETO_API_KEY:-}" ]] && echo "API key is set" || echo "API key is not set"
```

Clear it after use:

```bash
unset IMGBB_API_KEY
unset CHEVERETO_API_KEY
```

Migration commands in this terminal session now read the selected provider's
key automatically. The variable expires when the terminal session closes.

The key is needed only for `--apply`. Scanning and opening the GUI do not
require it to be set in advance.

Passing `--api-key` directly also works, but its value may be stored in shell
history or process listings. This Chevereto example uses PicGo.net:

```bash
python3 img_link_migrator.py \
  --apply \
  --provider chevereto \
  --api-key "YOUR_CHEVERETO_API_KEY" \
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

That command uses ImgBB. To upload to PicGo.net instead, set
`CHEVERETO_API_KEY` and select Chevereto:

```bash
python3 img_link_migrator.py \
  --apply \
  --provider chevereto \
  "$HOME/Documents/MyVault"
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

#### CLI modes and arguments

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
| `--provider NAME` | Use `imgbb` or `chevereto`. | `imgbb` | `--provider chevereto` selects PicGo.net. |
| `--chevereto-url URL` | Chevereto site base URL. | `https://www.picgo.net` | For another installation: `--provider chevereto --chevereto-url "https://images.example.com"`. |
| `--api-key KEY` | Replace `KEY` with the selected provider's API key. | Read `IMGBB_API_KEY` or `CHEVERETO_API_KEY`; apply fails if missing. | `--api-key "YOUR_KEY"`; the matching environment variable is safer. |
| `--expiration SECONDS` | Replace `SECONDS` with a number of seconds. | Default `0`, requesting permanent storage. | `60`–`15552000`; Chevereto receives an equivalent ISO 8601 duration. |
| `--retries N` | Replace `N` with the retry count. | Default `3`, meaning up to three retries after the first failure. | Range `0`–`10`; `--retries 0` makes one attempt. |
| `--include-host DOMAIN` | Replace `DOMAIN` with an allowed source domain. | Process every detected external image host. | `--include-host xhscdn.com`; repeat or comma-separate values. |
| `--exclude-host DOMAIN` | Replace `DOMAIN` with an excluded source domain. | Add no extra exclusions; the selected destination remains excluded. | `--exclude-host example.com`; repeat or comma-separate values. |
| `--state-dir PATH` | Replace `PATH` with the cache/backup directory. | Use the system app-data directory. | `--state-dir "./migrator-state"` |
| `--no-backup` | Flag; enter no value after it. | Back up Markdown before changes. | `--apply --no-backup "$HOME/Documents/MyVault"` |
| `--report PATH` | Replace `PATH` with a JSON report location. | Write no JSON report. | `--report "./report.json"` |
| `-h`, `--help` | Flag; enter no value after it. | Run normally. | `python3 img_link_migrator.py --help` |

### Python backups and state

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
URLs, provider namespaces, and expiration timestamps. It never contains API
keys.

### Supported Markdown syntax

```markdown
![alt text](https://example.com/image.png)

<img src="https://example.com/image.jpg" alt="Example">

![alt text][image-id]
[image-id]: https://example.com/image.webp
```

Normal links such as `[website](https://example.com/)` are intentionally not
treated as images.

### Python safety model

The migration pipeline is:

1. Scan Markdown and collect replaceable image URLs.
2. Download and validate the first unique source image.
3. Reuse a valid cache entry for the selected provider when possible;
   otherwise upload the image.
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
terminated during that interval, the selected provider may temporarily contain
an image not yet referenced by Markdown. The next run reuses the
provider-specific local cache and completes the replacement.

### Python upload expiration

The default expiration is `0`, which requests permanent storage. To request
automatic deletion, use a value from 60 through 15,552,000 seconds:

```bash
python3 img_link_migrator.py \
  --apply \
  --expiration 600 \
  "$HOME/Documents/MyVault"
```

Expired cache entries are not reused.

ImgBB receives the value as seconds. Chevereto receives the equivalent ISO
8601 duration, such as `PT600S`.

### Python development

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

Run a Python syntax check:

```bash
python3 -m py_compile img_link_migrator.py
```

The tests use fake ImgBB and Chevereto responses and do not perform uploads.

### Python tool limitations

- The Python GUI/CLI modifies only UTF-8 `.md` and `.markdown` files.
- Source images that require an authenticated browser session may fail to
  download. Xiaohongshu CDN requests automatically include the Xiaohongshu
  website as the HTTP referrer, which is sufficient for many public links.
- URL parsing focuses on common Obsidian and Markdown image syntax. Links
  generated by custom plugins with nonstandard syntax may not be detected.
- The Python tool does not delete images from any upload provider.

## Standalone single-file tool

`img-link-migrator.command` is a complete zsh program contained in one file.
It can be copied or moved out of this repository and run independently. At
runtime it neither reads nor launches `img_link_migrator.py`.

### Standalone scope

- Accept one `.txt`, `.md`, or `.markdown` file, or one directory.
- Search a directory recursively while skipping hidden files and directories.
- Scan file bytes directly without testing or converting the text encoding.
- Find `http://` and `https://` URLs anywhere in supported files.
- Filter URLs by source domain; the default is `xhscdn.com` and its subdomains.
- Upload to either ImgBB or PicGo.net using Chevereto API v1.
- Exclude existing links belonging to the selected destination.
- Download each selected URL and reject content that is not an image or is
  larger than 32 MB.
- Retry each download and upload up to four total attempts with automatic
  backoff.
- Reuse its own persistent source-URL cache.
- Immediately replace every occurrence of a successfully uploaded URL before
  processing the next image.

The standalone scanner is deliberately syntax-independent. In Markdown files,
it can find selected-domain URLs in image syntax, plain text, frontmatter, or
code blocks. Image validation prevents non-image downloads from being uploaded.
Use the Python program when Markdown-aware parsing rules are needed.

### Standalone requirements

- macOS
- An API key for ImgBB or PicGo.net
- The macOS system `zsh`, `curl`, `plutil`, `file`, `Perl`, and related command
  line tools

### Run the standalone tool

Double-click `img-link-migrator.command`, or run it from Terminal:

```bash
./img-link-migrator.command
```

The program asks only for the following information:

1. Select ImgBB or PicGo.net as the upload service.
2. Enter that service's API key. Typing is hidden for the current run.
3. Drag one supported file or directory into Terminal and press Return.
4. Choose the source domains. After scanning, migration starts immediately.

The upload-service prompt is:

```text
Choose the upload service:
  Press Return or type 1 for ImgBB.
  Type 2 for PicGo.net (Chevereto API v1).
Your choice [1]:
```

| Input | Upload destination |
| --- | --- |
| Press Return, `1`, or `imgbb` | ImgBB |
| `2`, `picgo`, `picgo.net`, or `chevereto` | PicGo.net |

For PicGo.net, the tool sends the image as the `source` multipart field to
`/api/1/upload` with the `X-API-Key` header.

The source explanation displayed by the program is:

```text
Choose where the original image links come from:
  Press Return to use xhscdn.com and its subdomains.
  Or type domains separated by commas: xhscdn.com,example.com
  Or type * to check every domain; non-image URLs are skipped.
Your choice [xhscdn.com]:
```

| Input at `Your choice` | URLs considered for migration |
| --- | --- |
| Press Return without typing | URLs from `xhscdn.com` and any of its subdomains. |
| `example.com` | URLs from `example.com` and its subdomains. |
| `xhscdn.com,example.com` | URLs from either listed domain and their subdomains. |
| `*` | URLs from every domain; only valid downloaded images are uploaded. |

There is no backup question and no start-confirmation question. The scan count
is displayed and processing begins immediately when matching URLs exist.

### Standalone replacement safety

The standalone tool creates no backup copies. Each individual file update is
written to a temporary file in the same directory and then installed with an
atomic rename:

1. A source image must download and upload successfully before replacement.
2. The file must still match the version recorded during the scan.
3. The complete replacement must be written successfully to the temporary
   file.
4. Only then is the original path atomically replaced.

If any of these steps fails, that replacement does not overwrite the file.
Replacements completed earlier remain on disk. After one image URL is written
to all matching files, the program starts the next image.

The standalone cache is stored at
`~/Library/Application Support/IMG Link Migrator Standalone/url-map.tsv`. It
contains provider namespaces plus source and destination URLs, not API keys.
Moving the `.command` file does not affect this cache.

### Standalone example

This content works in both text and Markdown files:

```text
Images
------------------------
1. https://sns-webpic-qc.xhscdn.com/path/to/image
```

After a successful upload, only the URL changes:

```text
Images
------------------------
1. https://i.ibb.co/example/image.webp
```

### Standalone development check

The self-test uses local temporary `.txt`, `.md`, and `.markdown` files,
including a file that is not valid UTF-8. It performs no network request:

```bash
zsh -n img-link-migrator.command
./img-link-migrator.command --self-test
```

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE).
