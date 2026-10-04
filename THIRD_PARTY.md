# Bundled components

The app uses SwiftUI and AppKit from macOS, a private CPython runtime, and the existing Python migration core. It does not ship a second GUI toolkit or a browser runtime.

| Component | Pinned version | License / source |
| --- | --- | --- |
| CPython (standalone distribution) | 3.13.16, release 20261003 | Python license; [distribution source and build instructions](https://github.com/astral-sh/python-build-standalone/tree/20261003), [CPython](https://github.com/python/cpython/tree/v3.13.16) |
| pyvips | 3.2.0 | MIT; [source](https://github.com/libvips/pyvips/tree/v3.2.0) |
| Pillow | 12.3.0 | HPND; fallback BMP/ICO/PSD decoding; [source](https://github.com/python-pillow/Pillow/tree/12.3.0) |
| pyvips-binary / libvips | 8.18.7 | Wrapper MIT; linked library/dependencies under their own licenses including LGPL; [wrapper](https://github.com/kleisauke/pyvips-binary/tree/v8.18.7), [binary build recipes](https://github.com/kleisauke/libvips-packaging/tree/v8.18.7), [libvips](https://github.com/libvips/libvips/tree/v8.18.7) |
| CFFI / pycparser | Resolved during build | MIT/BSD; notices from installed distributions are bundled |
| PyInstaller | 6.16.0 | GPL with bootloader distribution exception; [source](https://github.com/pyinstaller/pyinstaller/tree/v6.16.0) |

The libvips binary contains AV1/AVIF, HEIF/HEIC decoding, WebP, and other image codec components. Their exact versions, notices, and license texts are bundled in `Contents/Resources/Licenses/libvips-versions.json` and `libvips-THIRD-PARTY-NOTICES.md`, taken from the matching upstream binary release. HEIC encoding is not required; the bundled processing path outputs AVIF or WebP.

Package-distribution license files and Python license files are copied into `Contents/Resources/Licenses`. Source for the app and private processing core is also included in `Contents/Resources/Source`. Component source and build recipes are available at the links above. libvips is a separately linked dynamic library, not statically linked into the app executable. It can be rebuilt/replaced under `Contents/Helpers/migrator-engine/_internal/libvips.42.dylib`; rebuild the app with `scripts/build_app.py` to update code signatures after replacement. The app imposes no restriction on modifying or reverse engineering it for debugging modifications to LGPL-covered libraries.

This project remains MIT-licensed; dependency licenses are not replaced by the project license. Ad-hoc local packages are not Apple-notarized. Public distribution requires a suitable Developer ID signature and Apple notarization.
