"""Stock Market locked overlay mixin for EVE Market Scout.

Shows overlay when 3-year history not ready, with progress during import
and restart prompt when complete.

Expected parent attributes:
    frame: ttk.Frame - main Stock Market frame
"""

import tkinter as tk
from tkinter import ttk, messagebox

from background_import import get_background_import_status


class StockMarketOverlayMixin:
    """Mixin providing locked overlay for Stock Market tab."""
    
    def _create_locked_overlay(self):
        """Create overlay that covers tab when full history not ready."""
        # Overlay frame - placed over hub_notebook when locked
        self.locked_overlay = ttk.Frame(self.frame)
        
        # Center content
        center = ttk.Frame(self.locked_overlay)
        center.place(relx=0.5, rely=0.4, anchor=tk.CENTER)
        
        # Title
        self.overlay_title = ttk.Label(
            center,
            text="Preparing Stock Market Data",
            font=("Segoe UI", 14, "bold")
        )
        self.overlay_title.pack(pady=(0, 15))
        
        # Status message
        self.overlay_status = ttk.Label(
            center,
            text="Building 3-year price history...",
            font=("Segoe UI", 10)
        )
        self.overlay_status.pack(pady=(0, 10))
        
        # Progress bar
        self.overlay_progress_var = tk.DoubleVar(value=0)
        self.overlay_progress = ttk.Progressbar(
            center,
            variable=self.overlay_progress_var,
            length=300,
            mode="determinate"
        )
        self.overlay_progress.pack(pady=(0, 10))
        
        # Progress text (e.g., "450/1189 files")
        self.overlay_progress_text = ttk.Label(
            center,
            text="",
            font=("Segoe UI", 9),
            foreground="gray"
        )
        self.overlay_progress_text.pack(pady=(0, 20))
        
        # Restart button (hidden until needed)
        self.overlay_restart_btn = ttk.Button(
            center,
            text="Restart App to Activate",
            command=self._on_restart_prompt
        )
        # Not packed initially - shown when restart needed
        
        # Track lock state
        self._is_locked = False
        self._lock_poll_job = None
    
    def _poll_lock_state(self):
        """Poll background import status and update overlay."""
        try:
            status = get_background_import_status()
            
            # Check if profiles exist - if so, Stock Market is usable
            # even if restart is still needed (for scanner DB merge)
            if self._has_profiles():
                self._hide_overlay()
                return
            
            # Check if restart required (import complete, waiting for swap)
            if status.get('restart_required', False):
                self._show_restart_overlay()
                self._lock_poll_job = self.frame.after(10000, self._poll_lock_state)
                return
            
            # Check if background import is running
            if status.get('running', False):
                self._show_progress_overlay(status)
                self._lock_poll_job = self.frame.after(1000, self._poll_lock_state)
                return
            
            # Check if we have full history
            from market_history import get_market_history_db
            from gui_migration import check_has_full_history
            
            db = get_market_history_db()
            if check_has_full_history(db):
                self._hide_overlay()
                return
            
            # No full history and not importing - show waiting message
            self._show_waiting_overlay()
            self._lock_poll_job = self.frame.after(5000, self._poll_lock_state)
            
        except Exception as e:
            print(f"[StockMarket] Lock state poll error: {e}")
            self._hide_overlay()
    
    def _has_profiles(self):
        """Check if any profiles have been built."""
        try:
            profiles = self.profiles.get_all_profiles()
            return len(profiles) > 0
        except Exception:
            return False
    
    def _show_progress_overlay(self, status: dict):
        """Show overlay with import progress."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()
        
        self.overlay_title.configure(text="Preparing Stock Market Data")
        
        current = status.get('current', 0)
        total = status.get('total', 0)
        
        if total > 0:
            pct = (current / total) * 100
            self.overlay_progress_var.set(pct)
            self.overlay_progress_text.configure(text=f"{current}/{total} files")
            self.overlay_status.configure(text=status.get('status', 'Importing...'))
        else:
            self.overlay_status.configure(text=status.get('status', 'Starting import...'))
            self.overlay_progress_text.configure(text="")
        
        # Ensure progress bar visible, restart button hidden
        self.overlay_progress.pack(pady=(0, 10))
        self.overlay_progress_text.pack(pady=(0, 20))
        self.overlay_restart_btn.pack_forget()
    
    def _show_restart_overlay(self):
        """Show overlay prompting for restart."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()
        
        self.overlay_title.configure(text="Full History Ready!")
        self.overlay_status.configure(
            text="3-year price history has been built.\n"
                 "Restart the app to activate Stock Market features."
        )
        
        self.overlay_progress.pack_forget()
        self.overlay_progress_text.pack_forget()
        self.overlay_restart_btn.pack(pady=(10, 0))
    
    def _show_waiting_overlay(self):
        """Show overlay when no full history and not importing."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()
        
        self.overlay_title.configure(text="Stock Market Setup Required")
        self.overlay_status.configure(
            text="Stock Market features require 3-year price history.\n"
                 "Use the scanner - full history will download in background."
        )
        
        self.overlay_progress.pack_forget()
        self.overlay_progress_text.pack_forget()
        self.overlay_restart_btn.pack_forget()
    
    def _hide_overlay(self):
        """Hide the locked overlay - tab is usable."""
        if self._is_locked:
            self._is_locked = False
            self.locked_overlay.place_forget()
    
    def _on_restart_prompt(self):
        """Handle restart button click."""
        result = messagebox.askokcancel(
            "Restart Required",
            "The app needs to restart to activate full price history.\n\n"
            "Click OK to close the app. Then relaunch manually.",
            icon="info"
        )
        if result:
            try:
                self.frame.winfo_toplevel().destroy()
            except Exception:
                pass
