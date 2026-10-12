#!/bin/sh
# Builds a signed MySearch.app (with the native helper inside it) and packs it into build/MySearch.dmg, so macOS Full Disk
# Access can be granted to MySearch.app instead of Homebrew's Python (see README, "Install the app (DMG)").
#   ./scripts/build_dmg.sh                       sign with MSE_SIGN_IDENTITY (default: ad-hoc), write ./build/MySearch.dmg
#   ./scripts/build_dmg.sh --adhoc --out <dir>   ad-hoc signature, output in <dir> (a test build; no Keychain prompt)
#   ./scripts/build_dmg.sh --bundle-runtime ...  also put the Python runtime, the locked dependencies and mse inside the app
#                                                (scripts/bundle_runtime.sh): the DMG then runs on a Mac with no clone and no venv.
#                                                That image carries GPL code (FFmpeg and others, inside the opencv-python wheel):
#                                                its release MUST also attach MySearch-<version>-sources.tar.gz, made by
#                                                scripts/fetch_gpl_sources.sh and checked by scripts/check_gpl_release.sh
#                                                (docs/RELEASING.md). This script prints that reminder and, next to the image,
#                                                writes <image>.bundled-runtime, which the check script reads.
#   ./scripts/build_dmg.sh --hardened ...        hardened runtime + secure timestamp + entitlements (what notarization needs;
#                                                needs a real identity and network access to Apple's timestamp server)
#   ./scripts/build_dmg.sh --app-only ...        stop after the signed app (scripts/notarize.sh notarizes and staples it, then
#   ./scripts/build_dmg.sh --dmg-only ...        packs the stapled app: the app already in <out> is verified and packed)
#   ./scripts/build_dmg.sh --versioned ...       name the image MySearch-<version>.dmg (the name the Homebrew cask expects)
# Without --hardened: no hardened runtime, no notarization (local use). Signing is inside-out (the runtime's Mach-O files,
# helper, Finder extension, then app), never --deep.
# Signing values come from the environment, or from scripts/sign.local.env (git-ignored) when that file exists (the environment wins):
#   MSE_SIGN_IDENTITY  Keychain identity name; "-" (the default) is an ad-hoc signature
#   MSE_SIGN_SHA1      that certificate's SHA-1 fingerprint, used to pick it in the Keychain (optional)
#   MSE_TEAM_ID        Apple Team ID (optional); when set, the signed app must report this TeamIdentifier
# See scripts/sign.local.env.example.
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
# values already set in the environment win over scripts/sign.local.env (scripts/notarize.sh relies on it)
pre_id="${MSE_SIGN_IDENTITY-__unset__}"; pre_sha="${MSE_SIGN_SHA1-__unset__}"; pre_team="${MSE_TEAM_ID-__unset__}"
if [ -f "$here/scripts/sign.local.env" ]; then . "$here/scripts/sign.local.env"; fi
[ "$pre_id" = __unset__ ] || MSE_SIGN_IDENTITY="$pre_id"
[ "$pre_sha" = __unset__ ] || MSE_SIGN_SHA1="$pre_sha"
[ "$pre_team" = __unset__ ] || MSE_TEAM_ID="$pre_team"
identity="${MSE_SIGN_IDENTITY:--}"
sha1="${MSE_SIGN_SHA1:-}"
team="${MSE_TEAM_ID:-}"
out="$here/build"
adhoc=""
bundle=""
hardened=""
app_only=""
dmg_only=""
versioned=""
while [ $# -gt 0 ]; do
  case "$1" in
    --adhoc) adhoc=1 ;;
    --bundle-runtime) bundle=1 ;;
    --hardened) hardened=1 ;;
    --app-only) app_only=1 ;;
    --dmg-only) dmg_only=1 ;;
    --versioned) versioned=1 ;;
    --out) shift; [ -n "$1" ] || { echo "--out needs a folder" >&2; exit 2; }; out="$1" ;;
    *) echo "unknown option: $1 (use --adhoc, --out <dir>, --bundle-runtime, --hardened, --app-only, --dmg-only, --versioned)" >&2; exit 2 ;;
  esac
  shift
done
[ -z "$app_only" ] || [ -z "$dmg_only" ] || { echo "--app-only and --dmg-only exclude each other" >&2; exit 2; }
mkdir -p "$out"
out="$(cd "$out" && pwd)"
app="$out/MySearch.app"

app_version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/mse/__init__.py" | head -n 1)"
# printed on stderr, so the last line on stdout stays the path of the app or the image (scripts/notarize.sh reads it)
gpl_reminder() {
  cat >&2 <<MSG

*** GPL NOTICE: this build bundles the Python runtime, and with it GPL-licensed code (FFmpeg, x264, x265, Xvid, vid.stab and
    Rubber Band, inside the opencv-python wheel). A release of this image MUST also carry the source of those libraries:
      scripts/fetch_gpl_sources.sh <new folder>      makes MySearch-$app_version-sources.tar.gz (attach it to the same GitHub release)
      scripts/check_gpl_release.sh <release folder>  fails if the image has no such archive next to it
    See docs/RELEASING.md and THIRD_PARTY_NOTICES.md ("GPL components in the self-contained DMG").
MSG
}
[ -z "$bundle" ] || gpl_reminder

if [ -n "$adhoc" ] || [ "$identity" = "-" ]; then
  sign="-"
else
  sign="${sha1:-$identity}"
  security find-identity -v -p codesigning | grep -q "$sign" || { echo "signing identity $identity ($sign) not found in the Keychain; use --adhoc for a test build" >&2; exit 1; }
fi
if [ -n "$hardened" ] && [ "$sign" = "-" ]; then
  echo "--hardened is for a real signing identity (notarization rejects ad-hoc signatures); set MSE_SIGN_IDENTITY" >&2
  exit 2
fi
ents="$here/scripts/entitlements"
flags="--timestamp=none"
[ -z "$hardened" ] || flags="--options runtime --timestamp"

if [ -z "$dmg_only" ]; then
  # 1. the app (build_app.sh signs ad hoc; re-signed below), then the helper inside it
  MSE_APP_DIR="$out" "$here/scripts/build_app.sh" ${bundle:+--bundle-runtime} >/dev/null
  "$here/scripts/build_helper.sh" "$app/Contents/MacOS" >/dev/null
  [ -x "$app/Contents/MacOS/msehelper" ] || { echo "helper missing from the app bundle" >&2; exit 1; }

  # 2. sign inside-out: the runtime's Mach-O files, helper, the Finder extension (sandbox entitlements), then the app; no --deep
  if [ -d "$app/Contents/Resources/runtime" ]; then
    "$here/scripts/bundle_runtime.sh" sign "$app" --identity "$sign" ${hardened:+--hardened} \
      ${hardened:+--entitlements "$ents/python-runtime.entitlements"}
  fi
  # shellcheck disable=SC2086
  codesign --force --sign "$sign" $flags --identifier com.mysearch.app.msehelper "$app/Contents/MacOS/msehelper"
  appex="$app/Contents/PlugIns/MySearchFinder.appex"
  [ -d "$appex" ] || { echo "Finder extension missing from the app bundle" >&2; exit 1; }
  # shellcheck disable=SC2086
  codesign --force --sign "$sign" $flags --identifier com.mysearch.app.findersync --entitlements "$here/app/FinderExtension/MySearchFinder.entitlements" "$appex"
  if [ -n "$hardened" ]; then
    # shellcheck disable=SC2086
    codesign --force --sign "$sign" $flags --identifier com.mysearch.app --entitlements "$ents/MySearch.entitlements" "$app"
  else
    codesign --force --sign "$sign" --identifier com.mysearch.app "$app"
  fi
fi

[ -d "$app" ] || { echo "no $app: run without --dmg-only first" >&2; exit 1; }
codesign --verify --strict --verbose=2 "$app/Contents/MacOS/msehelper"
codesign --verify --strict --verbose=2 "$app/Contents/PlugIns/MySearchFinder.appex"
codesign --verify --deep --strict --verbose=2 "$app"
codesign -dv "$app" 2>&1
if [ -n "$team" ] && [ "$sign" != "-" ]; then
  codesign -dv "$app" 2>&1 | grep -q "TeamIdentifier=$team" || { echo "signed app does not report TeamIdentifier=$team (MSE_TEAM_ID)" >&2; exit 1; }
fi
if [ -n "$app_only" ]; then
  [ -n "$bundle" ] || [ ! -d "$app/Contents/Resources/runtime" ] || gpl_reminder
  echo "$app"; exit 0
fi

# 3. the DMG: MySearch.app and an Applications shortcut
name="MySearch"
if [ -n "$versioned" ]; then
  ver="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist")"
  name="MySearch-$ver"
fi
dmg="$out/$name.dmg"
stage="$out/dmg-stage"
mkdir -p "$stage"
ditto "$app" "$stage/MySearch.app"
ln -sfn /Applications "$stage/Applications"
hdiutil create -volname MySearch -fs HFS+ -format UDZO -ov -srcfolder "$stage" "$dmg" >/dev/null
# an image whose app holds the runtime (also when it was packed with --dmg-only from an earlier --bundle-runtime build) gets a
# sidecar file that names the archive its release needs; scripts/check_gpl_release.sh reads it
if [ -d "$app/Contents/Resources/runtime" ]; then
  {
    printf 'image=%s\n' "$(basename "$dmg")"
    printf 'version=%s\n' "$app_version"
    printf 'runtime=bundled\n'
    printf 'requires=MySearch-%s-sources.tar.gz\n' "$app_version"
  } > "$dmg.bundled-runtime"
  gpl_reminder
fi
echo "$dmg"
