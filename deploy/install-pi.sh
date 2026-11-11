#!/usr/bin/env bash
# Install Freqon on Raspberry Pi OS (Bookworm or later, 32- or 64-bit).
# Run from the project directory:  sudo ./deploy/install-pi.sh
#
# Pi OS is Debian, so this runs the Linux installer, which uses nrsc5's NEON code on
# 32-bit Pi OS. Settings for a Pi 3 are in deploy/config.example.toml.
exec "$(dirname "$0")/install-linux.sh" "$@"
