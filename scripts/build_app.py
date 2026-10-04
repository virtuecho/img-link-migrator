"""Build separate native macOS bundles with pinned, self-contained runtimes."""
import argparse
import hashlib
import json
import pathlib
import platform
import plistlib
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
VERSION = "1.0.0"
PYTHON_RELEASE = "20261003"
PYTHON_VERSION = "3.13.16"
PYTHON_HASHES = {
    "arm64": "9e01f63bbb08576cd9c8bc2d0564d098cb30c8453a0cd4bcf6aef458f6d2a147",
    "x86_64": "b4dad38ba6a344555ccb71a1b08caad0a6c0dda88c5803658bc95bd7f04e9f5c",
}
# These archives also contain the codec publishers' consolidated license notices.
VIPS_HASHES = {
    "arm64": "ffe78b7072bfdb043cd38642aa487ace4452141b94db3fb5c078440a027d9277",
    "x86_64": "50f61425b0eb034d46a8fdf1142663ab68c69933ae1d23064829714b2f0e955c",
}


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], cwd=ROOT, check=True, **kwargs)


def download(url, path, digest=None):
    if not path.exists():
        temporary = path.with_suffix(".part")
        request = urllib.request.Request(url, headers={"User-Agent": "IMG-Link-Migrator-builder"})
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(path)
    if digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        path.unlink()
        raise RuntimeError("Downloaded archive checksum mismatch: " + path.name)


def build(architecture, identity=None, notary_profile=None):
    print("Building " + architecture, flush=True)
    if architecture == "arm64" and platform.machine() != "arm64":
        raise RuntimeError("Build the arm64 runtime on an Apple Silicon Mac.")
    prefix = ["/usr/bin/arch", "-" + architecture]
    runtime = BUILD / ("python-" + architecture)
    triple = "aarch64" if architecture == "arm64" else "x86_64"
    name = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{triple}-apple-darwin-install_only_stripped.tar.gz"
    archive = BUILD / name
    download(f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_RELEASE}/{name}",
             archive, PYTHON_HASHES[architecture])
    if not runtime.exists():
        runtime.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(runtime, filter="data")
    python = runtime / "python/bin/python3"
    environment = BUILD / ("venv-" + architecture)
    if not environment.exists():
        run(prefix + [python, "-m", "venv", environment])
    python = environment / "bin/python"
    run(prefix + [python, "-m", "pip", "install", "--disable-pip-version-check",
                  "pyvips[binary]==3.2.0", "pyvips-binary==8.18.7", "Pillow==12.3.0", "pyinstaller==6.16.0"])
    sites = json.loads(run(prefix + [python, "-c", "import site,json; print(json.dumps(site.getsitepackages()))"],
                           capture_output=True, text=True).stdout)
    site = pathlib.Path(next(p for p in sites if (pathlib.Path(p) / "pyvips_binary.dylibs").exists()))
    work = BUILD / ("freeze-" + architecture)
    freeze = BUILD / ("engine-" + architecture)
    args = prefix + [python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
                     "--name", "migrator-engine", "--target-arch", architecture,
                     "--distpath", freeze, "--workpath", work, "--specpath", BUILD,
                     "--paths", ROOT, "--hidden-import", "_libvips", "--hidden-import", "_cffi_backend",
                     "--add-binary", str(site / "pyvips_binary.dylibs/libvips.42.dylib") + ":."]
    if identity:
        args += ["--codesign-identity", identity]
    run(args + [ROOT / "app/backend.py"])
    destination = DIST / architecture / "IMG Link Migrator.app"
    if destination.exists():
        shutil.rmtree(destination)
    contents = destination / "Contents"
    executable = contents / "MacOS/IMG Link Migrator"
    resources = contents / "Resources"
    licenses = resources / "Licenses"
    executable.parent.mkdir(parents=True)
    licenses.mkdir(parents=True)
    (contents / "Helpers").mkdir()
    shutil.copytree(freeze / "migrator-engine", contents / "Helpers/migrator-engine", symlinks=True)
    # PyInstaller preserves a wheel's dotted dylib directory, which codesign
    # mistakes for a bundle. Its @rpath alias can instead point to a real file.
    internal = contents / "Helpers/migrator-engine/_internal"
    for directory in internal.rglob("*.dylibs"):
        for library in directory.iterdir():
            target = internal / library.name
            if target.exists() or target.is_symlink():
                target.unlink()
            shutil.move(library, target)
        directory.rmdir()
    run(["xcrun", "swiftc", "-parse-as-library", "-O", "-target", architecture + "-apple-macosx13.0",
         ROOT / "app/MigratorApp.swift", "-o", executable, "-framework", "SwiftUI", "-framework", "AppKit"])
    info = {
        "CFBundleName": "IMG Link Migrator", "CFBundleDisplayName": "IMG Link Migrator",
        "CFBundleIdentifier": "io.github.virtuecho.img-link-migrator",
        "CFBundleExecutable": "IMG Link Migrator", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSPrincipalClass": "NSApplication",
    }
    with (contents / "Info.plist").open("wb") as output:
        plistlib.dump(info, output)
    for name in ("README.md", "README_CN.md", "LICENSE", "THIRD_PARTY.md"):
        shutil.copy2(ROOT / name, resources / name)
    for distribution in site.glob("*.dist-info"):
        for path in distribution.rglob("*"):
            if path.is_file() and any(word in path.name.lower() for word in ("license", "copying", "notice")):
                target = licenses / distribution.name / path.relative_to(distribution)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    for path in (runtime / "python").rglob("*"):
        if path.is_file() and (path.name.lower().startswith("license") or "licenses" in path.parts):
            target = licenses / "Python" / path.relative_to(runtime / "python")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    vips = BUILD / ("vips-" + architecture + ".tar.gz")
    suffix = "arm64" if architecture == "arm64" else "x64"
    download(f"https://github.com/kleisauke/libvips-packaging/releases/download/v8.18.7/libvips-8.18.7-osx-{suffix}.tar.gz",
             vips, VIPS_HASHES.get(architecture))
    with tarfile.open(vips) as tar:
        for name in ("THIRD-PARTY-NOTICES.md", "versions.json"):
            with tar.extractfile(name) as source:
                (licenses / ("libvips-" + name)).write_bytes(source.read())
    # App-only sources make replacement/rebuilding of LGPL-linked components practical.
    sources = resources / "Source"
    sources.mkdir()
    for name in ("img_link_migrator.py", "image_processing.py", "app/backend.py", "app/MigratorApp.swift", "scripts/build_app.py", "pyproject.toml"):
        target = sources / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    sign = ["codesign", "--force", "--deep", "--sign", identity or "-"]
    if identity:
        sign += ["--options", "runtime", "--timestamp"]
    run(sign + [destination])
    run(["codesign", "--verify", "--deep", "--strict", destination])
    package = DIST / f"IMG-Link-Migrator-{VERSION}-{architecture}.zip"
    if package.exists():
        package.unlink()
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", destination, package])
    if notary_profile:
        run(["xcrun", "notarytool", "submit", package, "--keychain-profile", notary_profile, "--wait"])
        run(["xcrun", "stapler", "staple", destination])
        package.unlink()
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", destination, package])
    print(f"Built {package} ({package.stat().st_size / 1_000_000:.1f} MB)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("arm64", "x86_64", "all"), default=platform.machine())
    parser.add_argument("--sign", help="Developer ID Application signing identity")
    parser.add_argument("--notary-profile", help="Existing notarytool Keychain profile")
    args = parser.parse_args()
    if args.notary_profile and not args.sign:
        parser.error("Notarization requires --sign.")
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    for architecture in (("arm64", "x86_64") if args.arch == "all" else (args.arch,)):
        build(architecture, args.sign, args.notary_profile)


if __name__ == "__main__":
    main()
