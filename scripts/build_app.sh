#!/bin/sh
# Builds MySearch.app (ad-hoc signed). It never touches ~/Applications unless you ask for it:
#   ./scripts/build_app.sh --install     installs into ~/Applications and registers it with Launch Services
#   MSE_APP_DIR=<folder> ./scripts/build_app.sh   builds into <folder>, without registering it (test builds)
#   ./scripts/build_app.sh               builds into ./build/MySearch.app in this project
#   ./scripts/build_app.sh --bundle-runtime   also puts a relocatable Python runtime, the locked dependencies and the mse package
#                                        inside the app (scripts/bundle_runtime.sh), so it runs on a Mac with no clone and no
#                                        venv. Without the flag the app uses the project's .venv, as before.
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
install=""
bundle=""
for a in "$@"; do
  case "$a" in
    --install) install=1 ;;
    --bundle-runtime) bundle=1 ;;
    *) echo "unknown option: $a (accepted: --install, --bundle-runtime)" >&2; exit 2 ;;
  esac
done
if [ -n "$MSE_APP_DIR" ]; then
  out="$MSE_APP_DIR"
elif [ -n "$install" ]; then
  out="$HOME/Applications"
else
  out="$here/build"
fi
app="$out/MySearch.app"
build="$(mktemp -d)"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

# every app/*.swift (main.swift is the entry)
swiftc -O -swift-version 5 -target arm64-apple-macos14.0 -o "$app/Contents/MacOS/MySearch" "$here"/app/*.swift \
  -framework Cocoa -framework WebKit -framework Carbon -framework Quartz -framework QuickLookThumbnailing -framework ServiceManagement

# Finder Sync extension (app/FinderExtension/): MySearch.app/Contents/PlugIns/MySearchFinder.appex, sandboxed, ad-hoc signed here
# (build_dmg.sh re-signs it with the real identity, before the app). It is only registered with the system once the app is
# installed and the user switches it on (System Settings > General > Login Items & Extensions > Finder).
ext="$here/app/FinderExtension"
appex="$app/Contents/PlugIns/MySearchFinder.appex"
rm -rf "$appex"
mkdir -p "$appex/Contents/MacOS"
swiftc -O -swift-version 5 -target arm64-apple-macos14.0 -application-extension -parse-as-library -module-name MySearchFinder \
  -o "$appex/Contents/MacOS/MySearchFinder" "$ext"/*.swift \
  -framework Cocoa -framework FinderSync -framework Foundation \
  -Xlinker -e -Xlinker _NSExtensionMain
cp "$ext/Info.plist" "$appex/Contents/Info.plist"
codesign --force --sign - --identifier com.mysearch.app.findersync --entitlements "$ext/MySearchFinder.entitlements" "$appex" >/dev/null 2>&1 || true

# Icon: app/AppIcon-1024.png when it exists (the finished macOS icon on its 1024 px canvas);
# otherwise rasterise the SVG mark with Quick Look. Either way, build an .icns from a 1024 px master.
src=""
if [ -f "$here/app/AppIcon-1024.png" ]; then
  src="$build/master.png"
  sips -s format png -z 1024 1024 "$here/app/AppIcon-1024.png" --out "$src" >/dev/null
else
  qlmanage -t -s 1024 -o "$build" "$here/mse/ui/icon.svg" >/dev/null 2>&1 || true
  [ -f "$build/icon.svg.png" ] && src="$build/icon.svg.png"
fi
if [ -n "$src" ]; then
  set_="$build/AppIcon.iconset"; mkdir -p "$set_"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$src" --out "$set_/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$src" --out "$set_/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$set_" -o "$app/Contents/Resources/AppIcon.icns"
  touch "$app"                       # Finder and the Dock pick up the new icon
fi

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>MySearch</string>
  <key>CFBundleDisplayName</key><string>MySearch</string>
  <key>CFBundleIdentifier</key><string>com.mysearch.app</string>
  <key>CFBundleExecutable</key><string>MySearch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.3</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>MySearch asks Terminal to open a folder and start Claude Code or Codex in it.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>CFBundleURLTypes</key><array><dict>
    <key>CFBundleURLName</key><string>com.mysearch.app</string>
    <key>CFBundleURLSchemes</key><array><string>mysearch</string></array>
  </dict></array>
</dict></plist>
EOF
# Licence files: every build carries them, bundled runtime or not (the app holds MIT code adapted from FinderFlow, whose
# notice has to travel with it). They are copied over any earlier copy, before the app is sealed by codesign below.
# With --bundle-runtime, bundle_runtime.sh also copies the licence texts of the GPL/LGPL code in the wheels (Licenses/).
cp "$here/LICENSE" "$app/Contents/Resources/LICENSE"
cp "$here/THIRD_PARTY_NOTICES.md" "$app/Contents/Resources/THIRD_PARTY_NOTICES.md"
# the bundled runtime goes in before the seal: its Mach-O files are signed first (ad hoc here, build_dmg.sh signs them again)
if [ -n "$bundle" ]; then
  "$here/scripts/bundle_runtime.sh" install "$app" >&2
  "$here/scripts/bundle_runtime.sh" sign "$app" --identity - >&2
fi
codesign --force --sign - "$app" >/dev/null 2>&1 || true
# register the mysearch:// scheme (and the new icon) with Launch Services right away; only for --install, never for a test build
if [ -n "$install" ] && [ -z "$MSE_APP_DIR" ]; then
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app" >/dev/null 2>&1 || true
fi
rm -rf "$build"
echo "$app"
