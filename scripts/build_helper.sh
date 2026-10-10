#!/bin/sh
# Builds the native helper into the app data directory (or into the directory given as the first argument, e.g. a
# scratch folder to try a build without replacing the installed helper).
# The helper also holds the file-name crawler (`msehelper crawl`) and FSEvents (`msehelper watch`) of the whole-Mac search;
# Full Disk Access goes to MySearch.app when the service runs through it (README, "Install the app (DMG)"); scripts/build_dmg.sh
# bundles this helper into the app. Otherwise it goes to the service's Python binary.
# After a rebuild, the Keychain items of Ask's cloud providers were written by the previous binary: store the keys
# again (`mse providers set-key <id>`), or allow the new helper once when macOS asks.
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$HOME/Library/Application Support/MySearch/bin}"
mkdir -p "$out"
swiftc -O -o "$out/msehelper.new" "$here/helper/msehelper.swift" -framework PDFKit -framework Vision -framework CoreServices -framework Security && mv -f "$out/msehelper.new" "$out/msehelper"
echo "$out/msehelper"
