"""Shared window-sizing helper for dialogs.

Background: dialogs in this app historically hardcoded ``win.geometry("WxH")``,
with W/H eyeballed against the developer's Windows (Segoe UI) font metrics. On
Linux the same widgets render a few pixels taller per row (different default
fonts), so the content overflows the fixed height and the bottom widgets -- the
Save/Cancel buttons -- get clipped off-screen. Reported by an alpha tester on
CachyOS @ 1200x800 and reproduced on Linux Mint.

``fit_window()`` replaces the hardcoded geometry: it measures what the laid-out
content actually requests on the current machine, clamps that to the visible
screen, centers it over the parent, and makes the window resizable (plus a
minsize floor) so anything clamped is still reachable by dragging. Call it
AFTER the dialog's widgets have been created.
"""

import tkinter as tk


def fit_window(win, min_width: int = 0, min_height: int = 0,
               max_screen_frac: float = 0.9):
    """Size *win* to its content, clamped to the screen, centered, resizable.

    Drop-in replacement for a hardcoded ``win.geometry("WxH")`` call -- call it
    after all child widgets exist so the requested size is final.

    min_width / min_height: optional floors, for when the natural content is
        smaller than you want the dialog to open (e.g. to keep a familiar
        width). Height is best left to auto-fit -- that's the bug we're fixing.
    max_screen_frac: never open larger than this fraction of the screen.
    """
    win.update_idletasks()

    # Size the content actually wants on THIS machine (honouring any floor).
    width = max(win.winfo_reqwidth(), min_width)
    height = max(win.winfo_reqheight(), min_height)

    # Never open larger than the visible screen.
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    width = min(width, int(screen_w * max_screen_frac))
    height = min(height, int(screen_h * max_screen_frac))

    # Center over the parent window when it's on-screen, else center on screen.
    x = y = None
    try:
        parent = win.master.winfo_toplevel()
        if parent.winfo_ismapped() and parent.winfo_width() > 1:
            x = parent.winfo_x() + (parent.winfo_width() - width) // 2
            y = parent.winfo_y() + (parent.winfo_height() - height) // 2
    except (AttributeError, tk.TclError):
        x = y = None
    if x is None:
        x = (screen_w - width) // 2
        y = (screen_h - height) // 3

    # Keep the whole window on-screen.
    x = max(0, min(x, screen_w - width))
    y = max(0, min(y, screen_h - height))

    win.geometry(f"{width}x{height}+{x}+{y}")

    # Backstop: let the user drag to reveal anything that got clamped, but don't
    # let them shrink back below where the content fits.
    win.resizable(True, True)
    win.minsize(width, height)
