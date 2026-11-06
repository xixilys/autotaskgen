#!/bin/sh
# Install the progress CLI, snapshotter, patrol and nightly dream job on the Mac and load the launchd jobs.
#   sh tools/progress/install_mac.sh            copy + load
#   sh tools/progress/install_mac.sh --no-load  copy only
# Global stop without unloading: touch ~/.progress/PATROL_OFF
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.progress/bin"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$BIN" "$HOME/.progress/log" "$AGENTS"
cp "$SRC"/*.py "$BIN"/
chmod +x "$BIN"/*.py
printf '#!/bin/sh\nexec /usr/bin/python3 "$HOME/.progress/bin/progress.py" "$@"\n' > "$BIN/progress"
chmod +x "$BIN/progress"
printf '#!/bin/sh\nexec /usr/bin/python3 "$HOME/.progress/bin/progress_inspect.py" "$@"\n' > "$BIN/progress-inspect"
chmod +x "$BIN/progress-inspect"
for name in progress-snapshot progress-patrol progress-dream; do
  plist="com.example.$name.plist"
  cp "$SRC/launchd/$plist" "$AGENTS/$plist"
  if [ "${1:-}" != "--no-load" ]; then
    launchctl bootout "gui/$(id -u)/com.example.$name" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$plist"
  fi
done
# Hermes plugin progress-qq (QQ replies -> ~/.progress/inbox.jsonl): symlink only; config.yaml is NOT edited here.
if [ -d "$SRC/hermes_plugin/progress-qq" ]; then
  mkdir -p "$HOME/.hermes/plugins"
  # copy, not symlink: the launchd gateway cannot read files on /Volumes (macOS privacy)
  rm -rf "$HOME/.hermes/plugins/progress-qq"
  mkdir -p "$HOME/.hermes/plugins/progress-qq"
  cp "$SRC/hermes_plugin/progress-qq/plugin.yaml" "$SRC/hermes_plugin/progress-qq/__init__.py" "$HOME/.hermes/plugins/progress-qq/"
  echo "copied ~/.hermes/plugins/progress-qq"
  echo "NOTE: add progress-qq to plugins.enabled in ~/.hermes/config.yaml yourself, create ~/.progress/qq.json"
  echo '      ({"target": "qqbot:<openid>", "owner": "<openid>"}), then restart the Hermes gateway.'
fi
echo "installed to $BIN"
