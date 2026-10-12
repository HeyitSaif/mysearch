#!/bin/sh
# Signs MySearch for distribution outside the App Store, has Apple notarize it, and staples the tickets.
#
# SCAFFOLD. It has not been run. It needs a Developer ID certificate: if you have one, read it once before the first run,
# and run it on a Mac that has the certificate in its login Keychain.
#
#   MSE_DEVELOPER_ID=<codesign identity>  MSE_NOTARY_PROFILE=<notarytool keychain profile>  scripts/notarize.sh [--out <dir>] [--bundle-runtime]
#
#   MSE_DEVELOPER_ID     the codesign identity, e.g. "Developer ID Application: Your Name (TEAMID)" (security find-identity -v -p codesigning)
#   MSE_NOTARY_PROFILE   the name of a notarytool profile in the login Keychain. Make it once, yourself, with
#                          xcrun notarytool store-credentials <name> --apple-id <you> --team-id <TEAMID>
#                        which prompts for an app-specific password and keeps it in the Keychain. This script never sees, asks for
#                        or stores a credential; it refuses to run without both variables.
#   --bundle-runtime     ship the Python runtime inside the app (the DMG for people who have no clone and no venv)
#   --out <dir>          where the app and the disk image are built (default: build/notarized)
#
# What it does, in order (each step stops the script on failure):
#   1. scripts/build_dmg.sh --hardened --app-only --versioned: build and sign the app inside-out with the hardened runtime, a
#      secure timestamp and the entitlements in scripts/entitlements/ (MySearch.entitlements for the app; python-runtime.entitlements
#      for the bundled interpreter; the Finder extension keeps its sandbox entitlements). Every Mach-O file of the bundled
#      runtime is signed too: notarization rejects any unsigned or ad-hoc signed executable code, wherever it sits.
#   2. xcrun notarytool submit <app.zip> --wait, then xcrun stapler staple <app>.
#   3. scripts/build_dmg.sh --dmg-only --versioned: pack the stapled app into build/.../MySearch-<version>.dmg.
#   4. sign the DMG, notarytool submit --wait, stapler staple, then spctl and stapler validate checks.
#   5. print the DMG's sha256 (for packaging/homebrew/mysearch.rb).
#
# Entitlements (why, and what is unverified): scripts/entitlements/*.entitlements and docs/INSTALL.md, "Notarizing".
# MySearch.entitlements (the app): apple-events, for "Open in..." Terminal. python-runtime.entitlements (the bundled interpreter,
# the only executable that carries entitlements): disable-library-validation only. allow-jit and allow-unsigned-executable-memory
# are NOT set: with the hardened runtime on, the interpreter ran every package, an MLX GPU kernel and ctypes/cffi callbacks
# without them. That was checked with an ad-hoc signature only. Whether Apple's service accepts the whole bundle is not
# known until the first real run; if it rejects something, the log names the file:
#   xcrun notarytool log <submission-id> --keychain-profile "$MSE_NOTARY_PROFILE"
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
out="$here/build/notarized"
bundle=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle-runtime) bundle=1 ;;
    --out) shift; [ $# -gt 0 ] || { echo "--out needs a folder" >&2; exit 2; }; out="$1" ;;
    *) echo "usage: MSE_DEVELOPER_ID=... MSE_NOTARY_PROFILE=... scripts/notarize.sh [--out <dir>] [--bundle-runtime]" >&2; exit 2 ;;
  esac
  shift
done

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

if [ -z "${MSE_DEVELOPER_ID:-}" ] || [ -z "${MSE_NOTARY_PROFILE:-}" ]; then
  cat >&2 <<'MSG'
Refusing to run: MSE_DEVELOPER_ID and MSE_NOTARY_PROFILE must both be set.
  MSE_DEVELOPER_ID    your "Developer ID Application: ..." codesign identity
  MSE_NOTARY_PROFILE  a notarytool keychain profile (xcrun notarytool store-credentials <name> ...)
This script stores no credentials and asks for none. See the header of scripts/notarize.sh.
MSG
  exit 2
fi
case "$MSE_DEVELOPER_ID" in
  "Developer ID Application:"*) ;;
  *) die "MSE_DEVELOPER_ID must be a \"Developer ID Application: ...\" identity; notarization does not accept any other kind" ;;
esac
xcrun --find notarytool >/dev/null 2>&1 || die "xcrun notarytool not found (Xcode 13 or later)"
xcrun --find stapler >/dev/null 2>&1 || die "xcrun stapler not found"
security find-identity -v -p codesigning | grep -F -q "$MSE_DEVELOPER_ID" || die "identity not found in the Keychain: $MSE_DEVELOPER_ID"

mkdir -p "$out"
out="$(cd "$out" && pwd)"
app="$out/MySearch.app"

# values from the environment beat scripts/sign.local.env in build_dmg.sh
export MSE_SIGN_IDENTITY="$MSE_DEVELOPER_ID" MSE_SIGN_SHA1=""

# $1 = file to submit. Waits for Apple's verdict; the submission id and verdict are kept in <file>.notary.txt next to it.
submit() {
  log="$1.notary.txt"
  xcrun notarytool submit "$1" --keychain-profile "$MSE_NOTARY_PROFILE" --wait | tee "$log"
  grep -q "status: Accepted" "$log" || {
    id="$(sed -n 's/^ *id: *//p' "$log" | head -n 1)"
    die "notarization of $1 was not accepted. Why: xcrun notarytool log $id --keychain-profile \"$MSE_NOTARY_PROFILE\""
  }
}

say "1. build and sign the app (hardened runtime, secure timestamp)"
"$here/scripts/build_dmg.sh" --hardened --app-only --versioned --out "$out" ${bundle:+--bundle-runtime}

say "2. notarize and staple the app"
zip="$out/MySearch-app.zip"
ditto -c -k --keepParent "$app" "$zip"
submit "$zip"
xcrun stapler staple "$app"
xcrun stapler validate "$app"

say "3. pack the stapled app"
dmg="$("$here/scripts/build_dmg.sh" --dmg-only --versioned --out "$out" | tail -n 1)"
[ -f "$dmg" ] || die "no disk image was made"

say "4. sign, notarize and staple the disk image"
codesign --force --sign "$MSE_DEVELOPER_ID" --timestamp "$dmg"
submit "$dmg"
xcrun stapler staple "$dmg"
xcrun stapler validate "$dmg"
spctl --assess --type open --context context:primary-signature -v "$dmg"
spctl --assess --type execute -vv "$app"

say "5. done"
echo "$dmg"
echo "sha256 (for packaging/homebrew/mysearch.rb):"
shasum -a 256 "$dmg"
if [ -n "$bundle" ]; then
  echo
  echo "This image bundles GPL, LGPL and MPL-2.0 code. The GitHub release must also carry the source of those libraries:"
  echo "  scripts/fetch_gpl_sources.sh <new folder>      then attach MySearch-<version>-sources.tar.gz"
  echo "  scripts/check_gpl_release.sh $out              fails while the archive is missing (docs/RELEASING.md)"
fi
