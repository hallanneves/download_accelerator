#!/bin/bash
# Constroi o app "Download Accelerator" (macOS) e instala em /Applications.
# Rode depois de editar download_accelerator.py ou download_accelerator_gui.py.
#
# Requisitos: python3 com Tkinter (Homebrew: brew install python-tk@3.14).
set -e
cd "$(dirname "$0")"
SRC="$PWD"
WORK="$(mktemp -d)"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet pyinstaller
cp download_accelerator.py download_accelerator_gui.py "$WORK/"
cd "$WORK"
"$WORK/venv/bin/pyinstaller" --noconfirm --clean --windowed \
    --name "Download Accelerator" \
    --icon "$SRC/assets/icon.icns" \
    --osx-bundle-identifier com.hallanneves.download-accelerator \
    download_accelerator_gui.py
rm -rf "/Applications/Download Accelerator.app"
cp -R "dist/Download Accelerator.app" "/Applications/Download Accelerator.app"
rm -rf "$WORK"
echo "Pronto: /Applications/Download Accelerator.app"
