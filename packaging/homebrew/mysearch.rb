# TEMPLATE for a Homebrew cask. Not published anywhere: see README.md in this folder for how to submit it.
#
# Fill in, for each release, from the disk image that scripts/notarize.sh (or scripts/build_dmg.sh --versioned) made:
#   version  the release number, without the leading "v" (the app's CFBundleShortVersionString)
#   sha256   shasum -a 256 MySearch-<version>.dmg   (scripts/notarize.sh prints it)
# The release asset must be named MySearch-<version>.dmg and sit on the GitHub release tagged v<version>.
cask "mysearch" do
  version "0.1.3"
  sha256 "REPLACE_WITH_THE_SHA256_OF_MySearch-0.1.3.dmg"

  url "https://github.com/HeyitSaif/mysearch/releases/download/v#{version}/MySearch-#{version}.dmg"
  name "MySearch"
  desc "Private search for your own folders: words and meaning, English and German"
  homepage "https://github.com/HeyitSaif/mysearch"

  livecheck do
    url :url
    strategy :github_latest
  end

  # The models are MLX models and run on the Apple Silicon GPU; the app needs macOS 14.
  depends_on macos: ">= :sonoma"
  depends_on arch: :arm64

  # MySearch checks for a newer release by itself once a day (Settings > Mac App > Check for updates), but never installs one:
  # `brew upgrade --cask mysearch` does that.
  auto_updates false

  app "MySearch.app"

  # Quit the app first: the background service (a LaunchAgent) belongs to it.
  uninstall launchctl: "com.mysearch.daemon",
            quit:      "com.mysearch.app"

  # The indexes, models, config (with the access token) and logs. `brew uninstall --zap` removes them; plain uninstall keeps
  # them, so a reinstall finds the index again. Hugging Face's own cache (~/.cache/huggingface) is not MySearch's and stays.
  zap trash: [
    "~/Library/Application Support/MySearch",
    "~/Library/LaunchAgents/com.mysearch.daemon.plist",
    "~/Library/Logs/MySearch",
    "~/Library/Preferences/com.mysearch.app.plist",
    "~/Library/Saved Application State/com.mysearch.app.savedState",
  ]
end
