# Bundled components

The app uses SwiftUI and AppKit from macOS, a bundled CPython runtime, and the Python migration core.

| Component | Pinned version | License / source |
| --- | --- | --- |
| CPython (standalone distribution) | 3.13.16, release 20261003 | Python license; [distribution source and build instructions](https://github.com/astral-sh/python-build-standalone/tree/20261003), [CPython](https://github.com/python/cpython/tree/v3.13.16) |
| pyvips | 3.2.0 | MIT; [source](https://github.com/libvips/pyvips/tree/v3.2.0) |
| Pillow | 12.3.0 | HPND; fallback BMP/ICO/PSD decoding; [source](https://github.com/python-pillow/Pillow/tree/12.3.0) |
| pi-heif | 1.4.0 | BSD-3-Clause wrapper; libheif/libde265 under LGPL; [source](https://github.com/bigcat88/pillow_heif), bundled license texts included |
| pyvips-binary / libvips | 8.18.7 | Wrapper MIT; linked library/dependencies under their own licenses including LGPL; [wrapper](https://github.com/kleisauke/pyvips-binary/tree/v8.18.7), [binary build recipes](https://github.com/kleisauke/libvips-packaging/tree/v8.18.7), [libvips](https://github.com/libvips/libvips/tree/v8.18.7) |
| CFFI / pycparser | Resolved during build | MIT/BSD; notices from installed distributions are bundled |
| PyInstaller | 6.16.0 | GPL with bootloader distribution exception; [source](https://github.com/pyinstaller/pyinstaller/tree/v6.16.0) |

The libvips binary contains AV1/AVIF, HEIF header parsing, WebP, and other image codec components. HEVC pixel decoding uses pi-heif with libde265. Exact libvips component versions, notices, and license texts are bundled in `Contents/Resources/Licenses/libvips-versions.json` and `libvips-THIRD-PARTY-NOTICES.md`, taken from the matching upstream binary release. pi-heif and its libraries' notices are copied from the matching wheel's license files. The processing path outputs AVIF or WebP.

Package-distribution license files and Python license files are copied into `Contents/Resources/Licenses`. Source for the app and processing core is included in `Contents/Resources/Source`. Component source and build recipes are available at the links above. libvips is dynamically linked at `Contents/Helpers/migrator-engine/_internal/libvips.42.dylib`. After replacing it, rebuild the app with `scripts/build_app.py` to update code signatures.

The project uses the GNU Affero General Public License v3. Bundled components use the licenses listed in their notices.
