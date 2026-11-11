#!/usr/bin/env bash
# Builds nrsc5 under MSYS2 (MINGW64) for the Windows installer and copies libnrsc5.dll,
# librtlsdr.dll and every MinGW DLL they need into <prefix>/bin.
# Usage (from install-windows.ps1): bash -l build-nrsc5.sh /c/ProgramData/Freqon
set -euo pipefail

PREFIX="$1"
mkdir -p "$PREFIX/bin"
cd "$PREFIX"

pacman -S --needed --noconfirm git make autoconf automake patch tar xz gzip \
  mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-libtool mingw-w64-x86_64-pkgconf \
  mingw-w64-x86_64-rtl-sdr mingw-w64-x86_64-libusb mingw-w64-x86_64-fftw mingw-w64-x86_64-libao

NRSC5_COMMIT=631abc16f0c09537c3acedba65dd18a7d0c15866  # the nrsc5 Freqon is tested with
if [[ "$(git -C nrsc5 rev-parse HEAD 2>/dev/null)" != "$NRSC5_COMMIT" ]]; then
  rm -rf nrsc5
  git init -q nrsc5
  git -C nrsc5 fetch -q --depth 1 https://github.com/theori-io/nrsc5.git "$NRSC5_COMMIT"
  git -C nrsc5 -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
# The system rtl-sdr (a DLL), not nrsc5's static copy: Freqon calls rtl-sdr directly to list
# dongles and find one by serial number.
cmake -S nrsc5 -B nrsc5/build -G "MSYS Makefiles" -DCMAKE_BUILD_TYPE=Release -DUSE_SSE=ON \
  -DUSE_SYSTEM_RTLSDR=ON -DUSE_SYSTEM_LIBUSB=ON -DUSE_SYSTEM_FFTW=ON -DUSE_SYSTEM_LIBAO=ON
cmake --build nrsc5/build -j"$(nproc)"

cp nrsc5/build/src/libnrsc5.dll /mingw64/bin/librtlsdr.dll bin/
# Their dependencies from MinGW (libusb, fftw, libao, the gcc runtime), found with ldd.
for _ in 1 2 3; do
  for dll in bin/*.dll; do ldd "$dll"; done | awk '$3 ~ /^\/mingw64\// {print $3}' | sort -u |
    while read -r dep; do cp -n "$dep" bin/; done
done
ls bin
