# Homebrew cask for MySearch

`mysearch.rb` is a **template**. Nothing here is published, and nothing in this repository talks to Homebrew.

## What it needs before it can work

1. **A release disk image** named `MySearch-<version>.dmg` attached to a GitHub release tagged `v<version>` on
   `HeyitSaif/mysearch`. Build it with `scripts/notarize.sh --bundle-runtime` (signed with a Developer ID, notarized and
   stapled), which also prints the sha256. An ad-hoc image is not enough: macOS blocks it on other Macs, so a cask
   that installs it would not open for the people who run it.
2. **The sha256** of that file in the `sha256` line, and the `version` line set to the release number (no `v`).

## Publishing it

Pick one:

- **Your own tap (quickest, no review).** Make a repository named `homebrew-tap` under your account, put the file at
  `Casks/mysearch.rb`, and people install with:

  ```sh
  brew tap HeyitSaif/tap
  brew install --cask mysearch
  ```

- **The official `homebrew/cask` tap (review by Homebrew's maintainers).** Fork `Homebrew/homebrew-cask`, copy the file to
  `Casks/m/mysearch.rb`, then from the fork:

  ```sh
  brew audit --cask --new mysearch
  brew style --fix Casks/m/mysearch.rb
  brew install --cask ./Casks/m/mysearch.rb
  ```

  and open a pull request. The official tap expects a notarized app, a project that is already known and in active
  use, and a stable download URL.

## Keeping it current

`livecheck` follows the latest GitHub release, so `brew livecheck mysearch` reports a new version. For each release, change
`version` and `sha256` together. The app's own once-a-day update notice (Settings > Mac App) points people at the release
page; with the cask installed, `brew upgrade --cask mysearch` is the way to update.

## Uninstalling

`brew uninstall --cask mysearch` removes the app and stops the background service. `brew uninstall --zap --cask mysearch`
also removes the indexes, models and logs listed under `zap`. Neither removes Full Disk Access: switch MySearch off in
System Settings > Privacy & Security > Full Disk Access yourself.
