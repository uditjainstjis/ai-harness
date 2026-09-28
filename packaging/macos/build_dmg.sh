#!/usr/bin/env bash
# Builds dist/Pramana-<version>-macos-<arch>.dmg: Pramana Studio as a double-click Mac app
# (Python, Pramana and a native WebKit window bundled; nothing to install except git/python3 from
# Xcode Command Line Tools, which macOS offers on first use). The API key is pasted into the app.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
BV=${PRAMANA_BUILD_VENV:-$ROOT/build/appenv}
PY=${PRAMANA_BUILD_PYTHON:-python3.12}
command -v "$PY" >/dev/null || PY=python3

echo "==> build environment: $BV"
if command -v uv >/dev/null; then
  uv venv -q --allow-existing --python "$PY" "$BV"
  uv pip install -q --python "$BV/bin/python" . pyinstaller pywebview
else
  "$PY" -m venv "$BV"
  "$BV/bin/python" -m pip install -q . pyinstaller pywebview
fi
VER=$("$BV/bin/python" -c 'import importlib.metadata as m; print(m.version("pramana"))')
ARCH=$(uname -m)

echo "==> Pramana.app $VER ($ARCH)"
rm -rf build/pyi dist/Pramana.app
"$BV/bin/pyinstaller" --noconfirm --clean --log-level WARN --windowed --name Pramana \
  --icon "$ROOT/packaging/macos/Pramana.icns" --osx-bundle-identifier dev.pramana.studio \
  --workpath build/pyi --distpath dist --specpath build/pyi \
  --add-data "$ROOT/pramana.toml:." --add-data "$ROOT/bench/tasks:bench/tasks" \
  --collect-data pramana --collect-submodules pramana \
  "$ROOT/packaging/macos/launcher.py"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" dist/Pramana.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c "Add :NSHumanReadableCopyright string 'Pramana — proof-carrying coding agent'" dist/Pramana.app/Contents/Info.plist 2>/dev/null || true
codesign --force --deep --sign - dist/Pramana.app      # ad-hoc: required to run on Apple Silicon

echo "==> disk image"
DMG="dist/Pramana-$VER-macos-$ARCH.dmg"
STAGE=build/dmg
rm -rf "$STAGE" "$DMG"; mkdir -p "$STAGE"
cp -R dist/Pramana.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp packaging/macos/READ_ME_FIRST.txt "$STAGE/"
hdiutil create -quiet -volname "Pramana $VER" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo "==> $DMG ($(du -h "$DMG" | cut -f1))"
