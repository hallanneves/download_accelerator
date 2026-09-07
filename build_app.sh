#!/bin/bash
# Constroi o app "Download Accelerator" (macOS), instala em /Applications e
# gera o zip para a release em dist/.
# Rode depois de editar download_accelerator.py ou download_accelerator_gui.py.
#
# Requisitos: python3 com Tkinter (Homebrew: brew install python-tk@3.14).
set -e
cd "$(dirname "$0")"
SRC="$PWD"
VERSION="$(python3 -c 'import download_accelerator as d; print(d.__version__)')"
ARCH="$(uname -m)"
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

APP="dist/Download Accelerator.app"
PLIST="$APP/Contents/Info.plist"
plist_set() {  # define a chave, criando se nao existir
    /usr/libexec/PlistBuddy -c "Set :$1 $3" "$PLIST" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"
}
plist_set CFBundleShortVersionString string "$VERSION"
plist_set CFBundleVersion string "$VERSION"
plist_set NSHumanReadableCopyright string "Copyright 2026 Hallan Neves. PolyForm Noncommercial 1.0.0."
codesign --force --deep --sign - "$APP"   # reassina depois de mexer no Info.plist

rm -rf "/Applications/Download Accelerator.app"
cp -R "$APP" "/Applications/Download Accelerator.app"

mkdir -p "$SRC/dist"
ZIP="$SRC/dist/Download-Accelerator-$VERSION-macos-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"   # preserva o bundle como o Finder faz

rm -rf "$WORK"
echo "Instalado: /Applications/Download Accelerator.app (versao $VERSION)"
echo "Release:   $ZIP"
