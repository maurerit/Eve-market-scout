"""Thread-safe task queue for Tk UI updates.

All background threads use submit() to schedule UI work on the main thread.
Main thread polls via root.after() during mainloop.

Usage from any thread:
    from tk_queue import submit
    submit(lambda: my_label.configure(text="Done"))

Setup in main.py (once, before mainloop):
    from tk_queue import start_polling
    start_polling(root)
"""

import queue


_queue = queue.Queue()


def submit(func):
    """Submit a function to run on the main thread.
    
    Thread-safe. Can be called from any thread.
    The function will execute during the next poll cycle (~50ms).
    """
    _queue.put(func)


def drain():
    """Drain and execute all pending tasks. Call from main thread only."""
    while True:
        try:
            func = _queue.get_nowait()
            try:
                func()
            except Exception as e:
                print(f"[TkQueue] Error executing task: {e}")
        except queue.Empty:
            break


def start_polling(root, interval_ms=50):
    """Start polling the queue from root's after() loop.
    
    Call once from main thread, after root is created but before mainloop().
    The poll runs inside mainloop via root.after(), which is safe because
    root.after() is called FROM the main thread.
    """
    def _poll():
        drain()
        root.after(interval_ms, _poll)
    
    root.after(interval_ms, _poll)
    print("[TkQueue] Polling started")
