#!/usr/bin/env bash
# Install Freqon on macOS (Apple silicon or Intel), for the current user. Needs Homebrew.
#
#   ./deploy/install-mac.sh               # install, and start Freqon now and at every login
#   ./deploy/install-mac.sh --no-service  # install only
#   ./deploy/install-mac.sh --uninstall   # stop it and remove the login item (keeps settings)
#
# Builds nrsc5 from source and installs Freqon into ~/Library/Freqon.
# Settings: ~/.config/freqon/config.toml. Stations and logos: ~/.local/share/freqon.
# Log: ~/Library/Logs/Freqon/freqon.log
set -euo pipefail

SERVICE=1
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-service) SERVICE=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -eq 0 ]]; then
  echo "Run as yourself, not with sudo: Freqon runs as your user on macOS." >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${FREQON_PREFIX:-$HOME/Library/Freqon}"
if [[ "$PREFIX" == *" "* ]]; then
  echo "The install folder can't contain spaces (nrsc5's build breaks): $PREFIX" >&2
  echo "Choose another with FREQON_PREFIX=/path ./deploy/install-mac.sh" >&2
  exit 1
fi
LABEL=org.freqon.server
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/Freqon"

if [[ $UNINSTALL == 1 ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Freqon stopped and removed from login items."
  echo "Program files: $PREFIX (delete by hand if you like). Settings and stations were kept."
  exit 0
fi

if ! command -v brew >/dev/null; then
  echo "Homebrew is needed: https://brew.sh" >&2
  exit 1
fi

echo "==> Installing packages"
# Only what's missing: never upgrade formulas you already have.
missing=()
for f in cmake autoconf automake libtool pkgconf fftw librtlsdr libao ffmpeg python@3.12; do
  brew list --formula "$f" &>/dev/null || missing+=("$f")
done
command -v git >/dev/null || missing+=(git)
if (( ${#missing[@]} )); then
  HOMEBREW_NO_INSTALL_UPGRADE=1 brew install "${missing[@]}"
else
  echo "all present"
fi
PYTHON="$(brew --prefix python@3.12)/bin/python3.12"

echo "==> Building nrsc5"
mkdir -p "$PREFIX"
NRSC5_COMMIT=631abc16f0c09537c3acedba65dd18a7d0c15866  # the nrsc5 Freqon is tested with
if [[ "$(git -C "$PREFIX/nrsc5" rev-parse HEAD 2>/dev/null)" != "$NRSC5_COMMIT" ]]; then
  rm -rf "$PREFIX/nrsc5"
  git init -q "$PREFIX/nrsc5"
  git -C "$PREFIX/nrsc5" fetch -q --depth 1 https://github.com/theori-io/nrsc5.git "$NRSC5_COMMIT"
  git -C "$PREFIX/nrsc5" -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
cmake -S "$PREFIX/nrsc5" -B "$PREFIX/nrsc5/build" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="$(brew --prefix)"
cmake --build "$PREFIX/nrsc5/build" -j"$(sysctl -n hw.ncpu)"
NRSC5_LIB="$PREFIX/nrsc5/build/src/libnrsc5.dylib"
[[ -f "$NRSC5_LIB" ]] || { echo "libnrsc5.dylib was not built" >&2; exit 1; }

echo "==> Installing Freqon"
[[ -x "$PREFIX/venv/bin/python" ]] || "$PYTHON" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$SRC"

# Settings from before the Freqon name move over; otherwise start from the example.
if [[ ! -e "$HOME/.config/freqon" && -d "$HOME/.config/hdradio" ]]; then
  mv "$HOME/.config/hdradio" "$HOME/.config/freqon"
fi
mkdir -p "$HOME/.config/freqon"
if [[ ! -f "$HOME/.config/freqon/config.toml" ]]; then
  # The Linux example's paths don't apply here: the data folder and library come from defaults
  # and the command line below.
  sed -e '/^data_dir = /d' -e '/^nrsc5_lib = /d' "$SRC/deploy/config.example.toml" \
    > "$HOME/.config/freqon/config.toml"
  chmod 600 "$HOME/.config/freqon/config.toml"
fi

if [[ $SERVICE == 1 ]]; then
  echo "==> Starting Freqon now and at every login"
  mkdir -p "$LOGS" "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PREFIX/venv/bin/freqon</string>
    <string>--nrsc5-lib</string><string>$NRSC5_LIB</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(brew --prefix)/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- Restart after a stalled dongle (exit 75) or a crash, not after a clean stop. -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <!-- Without this, macOS runs the agent at background priority and the dongle's USB reads
       fall behind during a scan's rapid retuning: nrsc5 reports the device lost. -->
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/freqon.log</string>
  <key>StandardErrorPath</key><string>$LOGS/freqon.log</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo
  echo "Freqon is starting: http://$(scutil --get LocalHostName 2>/dev/null || hostname).local:8080/"
  echo "Log: $LOGS/freqon.log"
  echo "If macOS asks whether Python may accept incoming connections, choose Allow."
else
  echo
  echo "Freqon is installed. Run it with:"
  echo "  \"$PREFIX/venv/bin/freqon\" --nrsc5-lib \"$NRSC5_LIB\""
fi
