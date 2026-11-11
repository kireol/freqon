#!/usr/bin/env bash
# Install Freqon on Linux: Debian, Ubuntu, Raspberry Pi OS (apt), Fedora (dnf) or Arch (pacman).
#
#   sudo ./deploy/install-linux.sh              # install and start the freqon service
#   sudo ./deploy/install-linux.sh --no-service # install only (containers, testing)
#
# Builds nrsc5 from source, installs Freqon into /opt/freqon (a Python virtualenv), lets the
# freqon user open RTL-SDR dongles, and runs it as a systemd service on port 8080.
set -euo pipefail

SERVICE=1
for arg in "$@"; do
  case "$arg" in
    --no-service) SERVICE=0 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${FREQON_PREFIX:-/opt/freqon}"
. /etc/os-release
FAMILY="${ID} ${ID_LIKE:-}"

echo "==> Installing packages"
case "$FAMILY" in
  *debian*|*ubuntu*|*raspbian*)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    # numpy/scipy from the distro: prebuilt (compiling scipy on a Pi 3 takes hours).
    apt-get install -y --no-install-recommends \
      git ca-certificates cmake build-essential pkg-config autoconf automake libtool patch \
      librtlsdr-dev libusb-1.0-0-dev libfftw3-dev libao-dev \
      ffmpeg python3 python3-venv python3-pip python3-numpy python3-scipy
    ;;
  *fedora*|*rhel*|*centos*)
    dnf install -y git cmake gcc gcc-c++ make pkgconf-pkg-config autoconf automake libtool patch \
      rtl-sdr-devel libusb1-devel fftw-devel libao-devel \
      python3 python3-pip python3-numpy python3-scipy
    # Fedora's own ffmpeg lacks the H.264 encoder Plex/Jellyfin video needs; RPM Fusion has it.
    dnf install -y ffmpeg || dnf install -y ffmpeg-free
    ;;
  *arch*)
    pacman -Sy --needed --noconfirm git cmake base-devel pkgconf autoconf automake libtool patch \
      rtl-sdr libusb fftw libao ffmpeg python python-pip python-numpy python-scipy
    ;;
  *)
    echo "Unsupported distribution ($ID). Install the build tools, rtl-sdr, fftw, libao, ffmpeg" >&2
    echo "and Python 3.11+ yourself, then follow the README's manual steps." >&2
    exit 1
    ;;
esac
ENCODERS="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
if [[ "$ENCODERS" != *libx264* ]]; then
  echo "WARNING: this ffmpeg has no libx264, so the Plex/Jellyfin video stream won't work." >&2
  echo "         On Fedora, enable RPM Fusion and install its ffmpeg." >&2
fi

echo "==> Keeping the DVB-T kernel driver away from the RTL-SDR"
install -d /etc/modprobe.d /etc/udev/rules.d
echo "blacklist dvb_usb_rtl28xxu" > /etc/modprobe.d/blacklist-rtlsdr.conf
modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true

echo "==> Building nrsc5"
mkdir -p "$PREFIX"
NRSC5_COMMIT=631abc16f0c09537c3acedba65dd18a7d0c15866  # the nrsc5 Freqon is tested with
if [[ "$(git -C "$PREFIX/nrsc5" rev-parse HEAD 2>/dev/null)" != "$NRSC5_COMMIT" ]]; then
  rm -rf "$PREFIX/nrsc5"
  git init -q "$PREFIX/nrsc5"
  git -C "$PREFIX/nrsc5" fetch -q --depth 1 https://github.com/theori-io/nrsc5.git "$NRSC5_COMMIT"
  git -C "$PREFIX/nrsc5" -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
NEON=OFF
# 32-bit ARM (armv7l, e.g. Pi OS 32-bit) can use nrsc5's NEON code; 64-bit ARM must not.
[[ "$(uname -m)" == armv7l ]] && NEON=ON
cmake -S "$PREFIX/nrsc5" -B "$PREFIX/nrsc5/build" -DUSE_NEON=$NEON -DCMAKE_BUILD_TYPE=Release
cmake --build "$PREFIX/nrsc5/build" -j"$(nproc)"
cmake --install "$PREFIX/nrsc5/build"
printf '/usr/local/lib\n/usr/local/lib64\n' > /etc/ld.so.conf.d/freqon.conf
ldconfig
NRSC5_LIB=""
for f in /usr/local/lib/libnrsc5.so /usr/local/lib64/libnrsc5.so; do
  [[ -z "$NRSC5_LIB" && -f "$f" ]] && NRSC5_LIB="$f"
done
[[ -n "$NRSC5_LIB" ]] || { echo "libnrsc5.so was not installed" >&2; exit 1; }

echo "==> Moving an install from before the Freqon name, if there is one"
if [[ -f /etc/systemd/system/hdradio.service ]]; then
  systemctl disable --now hdradio 2>/dev/null || true
  rm -f /etc/systemd/system/hdradio.service
fi
if [[ -d /etc/hdradio && ! -e /etc/freqon ]]; then
  mv /etc/hdradio /etc/freqon
  sed -i 's#/var/lib/hdradio#/var/lib/freqon#g' /etc/freqon/config.toml
fi
if [[ -d /var/lib/hdradio && ! -e /var/lib/freqon ]]; then
  mv /var/lib/hdradio /var/lib/freqon
fi

echo "==> Installing Freqon"
id freqon &>/dev/null || useradd --system --home /var/lib/freqon --shell /usr/sbin/nologin freqon
getent group plugdev >/dev/null && usermod -aG plugdev freqon
install -d -o freqon -g freqon /var/lib/freqon
chown -R freqon:freqon /var/lib/freqon
# Let the freqon user open RTL-SDR dongles, whatever group the distro's own rules use.
cat > /etc/udev/rules.d/60-freqon-rtlsdr.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832|2838", GROUP="freqon", MODE="0660"
EOF
udevadm control --reload-rules 2>/dev/null && udevadm trigger --subsystem-match=usb 2>/dev/null || true
python3 -m venv --system-site-packages "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$SRC"

install -d /etc/freqon
if [[ ! -f /etc/freqon/config.toml ]]; then
  # The config may hold a camera password: readable by root and the service only.
  install -m 640 -g freqon "$SRC/deploy/config.example.toml" /etc/freqon/config.toml
  sed -i "s#^nrsc5_lib = .*#nrsc5_lib = \"$NRSC5_LIB\"#" /etc/freqon/config.toml
fi

if [[ $SERVICE == 1 ]]; then
  sed "s#/opt/freqon#$PREFIX#g" "$SRC/deploy/freqon.service" > /etc/systemd/system/freqon.service
  systemctl daemon-reload
  systemctl enable --now freqon
  echo
  echo "Freqon is running: http://$(hostname).local:8080/"
  echo "Logs: journalctl -u freqon -f"
else
  echo
  echo "Freqon is installed (no service). Run it with:"
  echo "  sudo -u freqon $PREFIX/venv/bin/freqon --config /etc/freqon/config.toml"
fi
