#!/bin/bash
# Duplo clique abre a interface do acelerador de download.
cd "$(dirname "$0")"
if python3 -c "import tkinter" 2>/dev/null; then
    exec python3 download_accelerator_gui.py "$@"
else
    exec /usr/bin/python3 download_accelerator_gui.py "$@"
fi
