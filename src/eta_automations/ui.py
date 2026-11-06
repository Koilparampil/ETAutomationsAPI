"""Tk file picker for `eta run` without a CSV argument (11-cli §6) — port of
`tinkyWinky.get_user_inputs_authed`.

Only the input file is chosen here: credentials are never collected through the GUI any more, they come
from `.env` (02-configuration). `tkinter` is imported lazily inside pick_input_file(), so importing this
module (and the CLI) works on machines without Tk, e.g. headless Linux.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = ["NO_GUI_MESSAGE", "WINDOW_TITLE", "is_valid_csv_file", "pick_input_file"]

# 11 §6 — exact texts
NO_GUI_MESSAGE = "No input file given and no GUI available — pass the CSV path: eta run <file.csv>"
WINDOW_TITLE = "ETA Tool"
_LABEL = "FileName"
_ENTRY_WIDTH = 42
_DIALOG_TITLE = "Select .csv file"
_FILETYPES = [("CSV Files", "*.csv")]
_ERROR_TITLE = "Invalid file"


def is_valid_csv_file(path: str | Path | None) -> tuple[bool, str]:
    """11 §6 / 01 §2 (`tinkyWinky.is_valid_txt_file`): the path exists, is a file and ends in `.csv`
    (case-insensitive). Returns (ok, reason); reason is "" when ok."""
    text = str(path).strip() if path is not None else ""
    if not text:
        return False, "Please choose a .csv file."
    candidate = Path(text).expanduser()
    if not candidate.exists():
        return False, f"File not found: {candidate}"
    if not candidate.is_file():
        return False, f"Not a file: {candidate}"
    if candidate.suffix.lower() != ".csv":
        return False, f"Not a .csv file: {candidate}"
    return True, ""


def pick_input_file() -> Path | None:
    """Small Tk window titled 'ETA Tool' with: label 'FileName', entry (width 42), 'Browse...' button
    (filedialog.askopenfilename(title='Select .csv file', filetypes=[('CSV Files', '*.csv')])),
    'Cancel' and 'Submit' buttons; Enter submits, Esc cancels. Submit validates with
    is_valid_csv_file(path) -> (ok, reason); on failure messagebox.showerror('Invalid file', reason) and
    the window stays open. Returns the Path, or None if cancelled (11 §6).

    Raises ConfigError(NO_GUI_MESSAGE) when tkinter is not installed or no display is available.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except ImportError as exc:  # tkinter not installed (e.g. a minimal Linux Python)
        raise ConfigError(NO_GUI_MESSAGE) from exc
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display (headless session, SSH without X forwarding)
        raise ConfigError(NO_GUI_MESSAGE) from exc

    chosen: dict[str, Path | None] = {"path": None}
    file_name = tk.StringVar(master=root)

    def browse() -> None:
        selected = filedialog.askopenfilename(parent=root, title=_DIALOG_TITLE, filetypes=_FILETYPES)
        if selected:
            file_name.set(str(selected))

    def submit(_event: Any = None) -> None:
        value = file_name.get()
        ok, reason = is_valid_csv_file(value)
        if not ok:
            messagebox.showerror(_ERROR_TITLE, reason, parent=root)
            return  # stay open (11 §6)
        chosen["path"] = Path(value.strip()).expanduser()
        root.destroy()

    def cancel(_event: Any = None) -> None:
        chosen["path"] = None
        root.destroy()

    root.title(WINDOW_TITLE)
    tk.Label(root, text=_LABEL).grid(row=0, column=0, padx=8, pady=8, sticky="w")
    entry = tk.Entry(root, textvariable=file_name, width=_ENTRY_WIDTH)
    entry.grid(row=0, column=1, padx=4, pady=8)
    tk.Button(root, text="Browse...", command=browse).grid(row=0, column=2, padx=8, pady=8)
    tk.Button(root, text="Cancel", command=cancel).grid(row=1, column=1, padx=4, pady=8, sticky="e")
    tk.Button(root, text="Submit", command=submit).grid(row=1, column=2, padx=8, pady=8)
    root.bind("<Return>", submit)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)  # closing the window = Cancel
    entry.focus_set()
    root.mainloop()
    return chosen["path"]
