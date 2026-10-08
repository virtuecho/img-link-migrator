# IMG Link Migrator

[中文说明](README_CN.md)

A native macOS app that optimizes externally linked images, uploads them to ImgBB or PicGo.net, and replaces the links in Markdown and text documents. The app bundles its Python runtime and image codecs.

## Install

Requires macOS 13 or later. Choose the **single-architecture** download for your Mac:

| Download | Mac |
| --- | --- |
| `IMG-Link-Migrator-1.1.1-arm64.zip` | Apple Silicon: M1 and later |
| `IMG-Link-Migrator-1.1.1-x86_64.zip` | Intel |

Unzip and drag `IMG Link Migrator.app` to Applications.

## Use

1. Add one or more `.txt`, `.md`, or `.markdown` files, or folders. Folders are searched recursively. Drag and drop also works.
2. Choose **ImgBB** or **PicGo.net**, and enter your API key in the app's **API key** field.
3. Choose an image mode and any advanced options. **Size Limit** and document backups are enabled by default.
4. Click **Scan**. Review the files, image URLs, counts, and source domains, then uncheck domains to skip.
5. Click **Start Migration**. In confirmation dialogs, **Return accepts** and **Escape cancels**.
6. Review status and image properties. Sort with column headers or **Sort by**. Drag the divider below the list to resize its height, and column borders to resize widths. Select text to copy, use **Copy All**, or right-click a row to copy its details. Advanced Options can still be expanded during uploads.

Images are recognized in inline Markdown, referenced Markdown images whose definitions are used, and HTML `<img>` tags. Bare image URLs are recognized only in `.txt` files. Frontmatter, fenced code blocks, and inline code are skipped. The app preserves existing link text and document structure.

## Options

| Option | Default / behavior |
| --- | --- |
| Upload to | ImgBB or PicGo.net; default ImgBB |
| API key | Entered in the app's secure field; saved in macOS Keychain |
| Image mode | Size Limit |
| Back up documents | Enabled |
| Include domains | `xhscdn`: match host names containing this keyword; clear to include all eligible domains; comma-separated |
| Exclude domains | Empty; comma-separated; destination service domains also excluded |
| Source domain selection | All scanned domains enabled; uncheck a domain to skip |
| Image URLs | Always shown |
| Automatic retries | 3; adjustable from 0 to 10 |
| State directory | `~/Library/Application Support/IMG Link Migrator` |
| Clear Cache and Backups | Move the upload cache and document backups in the state directory to Trash; available while idle |
| Retry Failed | Retry failed images selected by the current domain filters |
| Stop | Stop starting new images; finish the current operation at a safe point |

Enter the key for the selected platform before starting migration. ImgBB and PicGo.net keys are stored separately in macOS Keychain and restored when the app opens or the platform changes. The last selected platform is remembered. Clearing the key field removes that platform's saved key. The backend redacts the current key from activity messages.

## Image rules

**Size Limit (default):** compression targets less than **1,000,000 bytes**. Processing failures attempt the original file; the host decides whether to accept it. **Original Upload:** supported images within the selected service's upload limit are passed through unchanged; oversized or unsupported images enter the same conversion process, using the service limit as their target.

A supported image already within its target limit uses the original file. Otherwise:

1. Try lossless AVIF. For ordinary 8-bit images, also try lossless WebP. Choose the smaller acceptable lossless result.
2. If neither fits, encode AVIF at quality **80**, then **70**, at the original dimensions.
3. If both are too large, reduce both dimensions by **15%**, then try **80 → 70** again.
4. Repeat as needed. Every candidate is generated from the original decoded image at the required scale, avoiding repeated lossy encoding of a previous candidate.

Compression stops after at most 32 dimension levels or when the smaller dimension reaches 16 pixels. Decode failures, conversion failures, animations, and unmet size targets fall back to uploading downloaded bytes unchanged, including damaged or oversized originals. There is no additional full-decode validation or output recheck. Download, upload, or document-write failures retain the original link. Download size is separately capped at 100,000,000 bytes; images requiring processing are capped at 100 megapixels. The size limit applies to the encoded image file, before the upload form is assembled.

Dimensions remain unchanged until the shrink step. AVIF supports 8, 10, and 12 bits and alpha; only ordinary 8-bit SDR images without NCLX are eligible for WebP.

- HDR JPEG/HEIC gain maps are recovered into HDR pixels and encoded as **12-bit BT.2020 / PQ AVIF**. HDR JPEG originals within the target are uploaded unchanged. HEIC gain-map conversion requires macOS 14 or later.
- Existing PQ/HLG AVIF retains its primaries and transfer function. AVIF gain-map recoding is unavailable; the original is still attempted when it cannot fit unchanged.
- libavif encodes the primary image's NCLX, retaining primaries and transfer values, including unspecified `2/2`. Matrix and range match the output encoding. Supported paths carry ICC profiles; encoded color tags are not manually patched.
- Lossless AVIF uses RGB identity and 4:4:4. Lossy AVIF uses 4:2:0 for 4:2:0 sources and 4:4:4 otherwise.

## Services and formats

| Service | Original image formats used by this app | Upload limit |
| --- | --- | --- |
| ImgBB | JPEG, PNG, BMP, GIF, WebP, AVIF, HEIC/HEIF, TIFF; decodable SVG, JPEG 2000, ICO and PSD | 32,000,000 bytes |
| PicGo.net | JPEG, PNG, BMP, GIF, WebP, AVIF | 25,000,000 bytes |

Formats are identified from image data. Uploads use indefinite retention, with automatic deletion disabled. Service references: [ImgBB uploader](https://imgbb.com/), [ImgBB API](https://api.imgbb.com/), [PicGo.net uploader](https://www.picgo.net/).

## Workflow

Scan → reuse cache or download → use original or compress → fall back to original bytes if processing fails → upload → back up and replace successful links. Failed uploads retain their source links and can be retried.

## Cache, retries, and document writes

- URL and processed-content caches are separated by platform, image mode, upload cap, and processing rule version. Only permanent upload entries are reused.
- Concurrency starts at 1 and grows through `1 → 3 → 5 → …`, up to 16 workers. Each explicit rate/concurrency refusal reduces it by 1 and disables further growth for that run. This is an observed ceiling, not a guarantee of the host's maximum. At most two images decode/encode simultaneously to limit memory. Upload attempts and retries still share a limiter of 50 per minute; throttling pauses retries.
- Links are replaced after successful upload or a valid cached result, when the document matches its scan or last successful write. Uploaded URLs remain available in the cache and image details.
- Enabled backups preserve each document before the task's first change, so the original links and text can be restored. Writes use a temporary file and atomic replacement.
- Stop/quit waits for a safe point. Completed uploads and writes are kept. A scan is required again after changing input files, platform, or domain filters.
- Encoders use temporary files that are removed after processing. URL/content mappings and document backups are stored in the state directory. **Clear Cache and Backups** moves `state.json` and `backups/` to Trash after confirmation. Restoring these items from Trash restores the local records; clearing them makes later tasks upload matching images again.

## Build from source

Requires macOS, Xcode Command Line Tools (`xcode-select --install`), a development Python 3.11 or later, and network access. Intel builds also require Yasm or NASM 2.x (for example, `brew install yasm`), plus Rosetta 2 on Apple Silicon. Intel Macs build the Intel package; use an Apple Silicon Mac for the arm64 package.

```sh
python3 scripts/build_app.py --arch arm64
python3 scripts/build_app.py --arch x86_64
# Build both on Apple Silicon:
python3 scripts/build_app.py --arch all
```

The builder downloads pinned, SHA-256-checked standalone Python runtimes into `build/`, creates architecture-specific environments, bundles pyvips/libvips, Pillow fallback loaders, and the pi-heif HEVC decoder with PyInstaller, builds libavif/libaom from source and compiles the native HDR helper and SwiftUI app, and assembles architecture-specific ZIPs in `dist/`.

The builder accepts a Developer ID identity through `--sign` and a configured notarytool Keychain profile through `--notary-profile`:

```sh
python3 scripts/build_app.py --arch arm64 --sign 'Developer ID Application: Your Name (TEAMID)' --notary-profile your-profile
```

With `--sign`, the builder signs the app and embedded executables. With `--notary-profile`, it submits the ZIP for notarization, staples the app, and rebuilds the ZIP. The default build uses an ad-hoc signature.

The bundles contain component license notices in `Contents/Resources/Licenses`. See [THIRD_PARTY.md](THIRD_PARTY.md) for versions, source links, and replacement/rebuild information. Project license: [GNU Affero General Public License v3](LICENSE).
