#!/bin/zsh
SCRIPT_DIR="${0:A:h}"

for candidate in \
  "${IMG_LINK_MIGRATOR_PYTHON:-}" \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  python3 \
  /usr/bin/python3
do
  [[ -z "$candidate" ]] && continue
  python_path="$(command -v "$candidate" 2>/dev/null)" || continue
  "$python_path" -c \
    'import tkinter as tk; root = tk.Tk(); root.withdraw(); root.update_idletasks(); root.destroy()' \
    >/dev/null 2>&1 || continue
  exec "$python_path" "$SCRIPT_DIR/img_link_migrator.py" --gui
done

echo "IMG Link Migrator could not find Python 3.9+ with a working Tk installation."
echo
echo "For Homebrew Python, find and install the Tk package matching your Python:"
echo "  brew search python-tk"
echo
echo "The CLI remains available without Tk:"
echo "  python3 \"$SCRIPT_DIR/img_link_migrator.py\" --help"
echo
read -k 1 "?Press any key to close..."
echo
exit 1
