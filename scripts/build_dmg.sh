#!/bin/sh
# Builds a signed MySearch.app (with the native helper inside it) and packs it into build/MySearch.dmg, so macOS Full Disk
# Access can be granted to MySearch.app instead of Homebrew's Python (see README, "Install the app (DMG)").
#   ./scripts/build_dmg.sh                       sign with MSE_SIGN_IDENTITY (default: ad-hoc), write ./build/MySearch.dmg
#   ./scripts/build_dmg.sh --adhoc --out <dir>   ad-hoc signature, output in <dir> (a test build; no Keychain prompt)
# Local use only: no hardened runtime, no notarization. Signing is inside-out (helper, Finder extension, then app), never --deep.
# Signing values come from the environment, or from scripts/sign.local.env (git-ignored) when that file exists:
#   MSE_SIGN_IDENTITY  Keychain identity name; "-" (the default) is an ad-hoc signature
#   MSE_SIGN_SHA1      that certificate's SHA-1 fingerprint, used to pick it in the Keychain (optional)
#   MSE_TEAM_ID        Apple Team ID (optional); when set, the signed app must report this TeamIdentifier
# See scripts/sign.local.env.example.
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$here/scripts/sign.local.env" ]; then . "$here/scripts/sign.local.env"; fi
identity="${MSE_SIGN_IDENTITY:--}"
sha1="${MSE_SIGN_SHA1:-}"
team="${MSE_TEAM_ID:-}"
out="$here/build"
adhoc=""
while [ $# -gt 0 ]; do
  case "$1" in
    --adhoc) adhoc=1 ;;
    --out) shift; [ -n "$1" ] || { echo "--out needs a folder" >&2; exit 2; }; out="$1" ;;
    *) echo "unknown option: $1 (use --adhoc, --out <dir>)" >&2; exit 2 ;;
  esac
  shift
done
mkdir -p "$out"
out="$(cd "$out" && pwd)"
app="$out/MySearch.app"
dmg="$out/MySearch.dmg"

if [ -n "$adhoc" ] || [ "$identity" = "-" ]; then
  sign="-"
else
  sign="${sha1:-$identity}"
  security find-identity -v -p codesigning | grep -q "$sign" || { echo "signing identity $identity ($sign) not found in the Keychain; use --adhoc for a test build" >&2; exit 1; }
fi

# 1. the app (build_app.sh signs ad hoc; re-signed below), then the helper inside it
MSE_APP_DIR="$out" "$here/scripts/build_app.sh" >/dev/null
"$here/scripts/build_helper.sh" "$app/Contents/MacOS" >/dev/null
[ -x "$app/Contents/MacOS/msehelper" ] || { echo "helper missing from the app bundle" >&2; exit 1; }

# 2. sign inside-out: helper, then the Finder extension (sandbox entitlements), then the app; no --deep, no hardened runtime
codesign --force --sign "$sign" --identifier com.mysearch.app.msehelper "$app/Contents/MacOS/msehelper"
appex="$app/Contents/PlugIns/MySearchFinder.appex"
[ -d "$appex" ] || { echo "Finder extension missing from the app bundle" >&2; exit 1; }
codesign --force --sign "$sign" --identifier com.mysearch.app.findersync --entitlements "$here/app/FinderExtension/MySearchFinder.entitlements" "$appex"
codesign --force --sign "$sign" --identifier com.mysearch.app "$app"
codesign --verify --strict --verbose=2 "$app/Contents/MacOS/msehelper"
codesign --verify --strict --verbose=2 "$appex"
codesign --verify --strict --verbose=2 "$app"
codesign -dv "$app" 2>&1
if [ -n "$team" ] && [ "$sign" != "-" ]; then
  codesign -dv "$app" 2>&1 | grep -q "TeamIdentifier=$team" || { echo "signed app does not report TeamIdentifier=$team (MSE_TEAM_ID)" >&2; exit 1; }
fi

# 3. the DMG: MySearch.app and an Applications shortcut
stage="$out/dmg-stage"
mkdir -p "$stage"
ditto "$app" "$stage/MySearch.app"
ln -sfn /Applications "$stage/Applications"
hdiutil create -volname MySearch -fs HFS+ -format UDZO -ov -srcfolder "$stage" "$dmg" >/dev/null
echo "$dmg"
