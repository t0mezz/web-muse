"""Desktop settings window (tkinter, stdlib only).

Shows the transcript mode (silent/verbose) from config.json and saves
the user's choice back through settings.py. Run with:
python settings_window.py
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from settings import TRANSCRIPT_MODES, get_transcript_mode, set_transcript_mode


def build_window(root: tk.Tk) -> dict:
    root.title("Settings")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=12)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frame, text="Session transcript").grid(row=0, column=0, sticky="w")
    mode = tk.StringVar(value=get_transcript_mode())
    combo = ttk.Combobox(
        frame, textvariable=mode, values=list(TRANSCRIPT_MODES),
        state="readonly", width=10,
    )
    combo.grid(row=0, column=1, padx=(8, 0))

    status = tk.StringVar()
    ttk.Label(frame, textvariable=status).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def on_save() -> None:
        set_transcript_mode(mode.get())
        status.set(f"Saved: transcript={mode.get()}")

    ttk.Button(frame, text="Save", command=on_save, default="active").grid(
        row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
    root.bind("<Return>", lambda _e: on_save())
    return {"mode": mode, "status": status, "save": on_save}


def main() -> None:
    root = tk.Tk()
    build_window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
