#!/bin/zsh
# service.sh — run The Faceless Studio (server.py) as a macOS background service (launchd).
# It starts at login, restarts itself if it crashes, and keeps rendering when the terminal /
# editor that started it is closed.
#
#   ./service.sh install     # install + start (http://localhost:5055)
#   ./service.sh status      # running? pid, current render
#   ./service.sh restart     # pick up code changes (refuses while a render is running)
#   ./service.sh logs        # follow the server log
#   ./service.sh uninstall   # stop + remove the service
set -e
LABEL="com.thefaceless.studio"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/logs/server.log"
PY="$(command -v python3)"
mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"

rendering() { [ -f "$DIR/static/outputs/.render.lock" ] && python3 -c "
import json,os,sys
try: os.kill(int(json.load(open('$DIR/static/outputs/.render.lock'))['pid']),0)
except Exception: sys.exit(1)" 2>/dev/null; }

case "$1" in
  install)
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-W</string><string>ignore</string><string>-u</string><string>$DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "✅ Installed + started: http://localhost:5055  (log: $LOG)";;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"; echo "🗑️  Service removed (renders already running keep going)";;
  restart)
    if rendering; then echo "⛔ A render is running — restart after it finishes (or use: $0 restart --force)"; [ "$2" = "--force" ] || exit 1; fi
    launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "🔄 Restarted";;
  status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state =|pid =" || echo "not installed"
    rendering && echo "🎬 rendering: $(python3 -c "import json;print(json.load(open('$DIR/static/outputs/.render.lock'))['episode'])")" || echo "idle";;
  logs) tail -f "$LOG";;
  *) echo "usage: $0 install|status|restart|logs|uninstall"; exit 1;;
esac
