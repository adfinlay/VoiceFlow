import sys
import time
import subprocess
import shutil
from services.logger import get_logger

log = get_logger("clipboard")

IS_LINUX = sys.platform.startswith('linux')
IS_WAYLAND = IS_LINUX and bool(
    __import__('os').environ.get('WAYLAND_DISPLAY')
    or __import__('os').environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'
)


def _find_tool(*names: str) -> str | None:
    """Find the first available tool from a list of names."""
    for name in names:
        if shutil.which(name):
            return name
    return None


class ClipboardService:
    def __init__(self):
        if IS_WAYLAND:
            self._paste_tool = _find_tool('wtype', 'dotool', 'ydotool')
            if self._paste_tool:
                log.info("Wayland paste tool found", tool=self._paste_tool)
            else:
                log.warning("No Wayland paste tool found (wtype/dotool/ydotool). "
                            "Falling back to pyautogui via XWayland.")
        else:
            self._paste_tool = None

        # pyautogui is imported lazily in _get_pyautogui() to avoid
        # mouseinfo's sys.exit() when tkinter is missing in bundled builds
        self._pyautogui = None

    def _get_pyautogui(self):
        """Lazy-load pyautogui to avoid mouseinfo's sys.exit() on missing tkinter."""
        if self._pyautogui is None:
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                pyautogui.PAUSE = 0.05
                self._pyautogui = pyautogui
            except (ImportError, SystemExit) as e:
                log.error("pyautogui unavailable", error=str(e))
                raise RuntimeError("pyautogui is not available - install tkinter or a Wayland paste tool (wtype/dotool/ydotool)")
        return self._pyautogui

    def copy_to_clipboard(self, text: str):
        """Copy text to clipboard."""
        if IS_WAYLAND:
            self._wl_copy(text)
        else:
            import pyperclip
            pyperclip.copy(text)

    def _wl_copy(self, text: str):
        """Copy text using wl-copy (Wayland native)."""
        try:
            subprocess.run(['wl-copy', '--', text], check=True, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            log.warning("wl-copy failed, falling back to pyperclip", error=str(e))
            import pyperclip
            pyperclip.copy(text)

    def _type_text_directly(self, text: str) -> bool:
        """Type text directly using Wayland input tool. Returns True on success."""
        try:
            if self._paste_tool == 'wtype':
                subprocess.run(['wtype', '--', text], check=True, timeout=10)
            elif self._paste_tool == 'dotool':
                # dotool type command types text directly
                subprocess.run(['dotool'], input=f'type {text}\n'.encode(),
                               check=True, timeout=10)
            elif self._paste_tool == 'ydotool':
                subprocess.run(['ydotool', 'type', '--', text],
                               check=True, timeout=10)
            else:
                return False
            log.debug("Text typed directly via", tool=self._paste_tool)
            return True
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("Direct type failed", tool=self._paste_tool, error=str(e))
            return False

    def _simulate_paste_keystroke(self, with_shift: bool = False):
        """Send the paste shortcut (Ctrl+V, or Ctrl+Shift+V for terminals)."""
        if IS_WAYLAND and self._paste_tool:
            try:
                if self._paste_tool == 'wtype':
                    if with_shift:
                        subprocess.run(
                            ['wtype', '-M', 'ctrl', '-M', 'shift', '-k', 'v',
                             '-m', 'shift', '-m', 'ctrl'],
                            check=True, timeout=5)
                    else:
                        subprocess.run(['wtype', '-M', 'ctrl', '-k', 'v', '-m', 'ctrl'],
                                       check=True, timeout=5)
                elif self._paste_tool == 'dotool':
                    combo = 'ctrl+shift+v' if with_shift else 'ctrl+v'
                    subprocess.run(['dotool'], input=f'key {combo}\n'.encode(),
                                   check=True, timeout=5)
                elif self._paste_tool == 'ydotool':
                    # 29=ctrl 42=shift 47=v
                    if with_shift:
                        subprocess.run(['ydotool', 'key',
                                        '29:1', '42:1', '47:1',
                                        '47:0', '42:0', '29:0'],
                                       check=True, timeout=5)
                    else:
                        subprocess.run(['ydotool', 'key',
                                        '29:1', '47:1', '47:0', '29:0'],
                                       check=True, timeout=5)
                log.debug("Paste keystroke sent via",
                          tool=self._paste_tool, with_shift=with_shift)
                return
            except (subprocess.SubprocessError, OSError) as e:
                log.warning("Paste tool failed, falling back to pyautogui",
                            tool=self._paste_tool, error=str(e))

        # Fallback: pyautogui (works via XWayland and on plain X11)
        if with_shift:
            self._get_pyautogui().hotkey('ctrl', 'shift', 'v')
        else:
            self._get_pyautogui().hotkey('ctrl', 'v')

    def paste_at_cursor(self, text: str, with_shift: bool = False):
        """Copy text to clipboard and paste at current cursor position.

        with_shift: send Ctrl+Shift+V instead of Ctrl+V (for terminals).
        Ignored on the Wayland direct-type path, which sends raw characters
        and bypasses paste shortcuts entirely.
        """
        log.debug("Paste at cursor called", text_length=len(text),
                  with_shift=with_shift)

        # Always copy to clipboard so user can re-paste manually if needed
        self.copy_to_clipboard(text)
        log.debug("Text copied to clipboard")

        # On Wayland, type text directly — works in terminals (Ctrl+V doesn't)
        # and all other apps, regardless of their paste shortcut.
        if IS_WAYLAND and self._paste_tool:
            if self._type_text_directly(text):
                log.debug("Paste complete via direct type")
                return
            # Direct type failed — fall through to keystroke simulation
            log.warning("Direct type failed, falling back to paste keystroke")

        # Small delay to ensure clipboard is ready
        time.sleep(0.1)

        log.debug("Simulating paste keystroke", with_shift=with_shift)
        self._simulate_paste_keystroke(with_shift=with_shift)
        log.debug("Paste command sent")

        time.sleep(0.1)

    def get_clipboard(self) -> str:
        """Get current clipboard content."""
        if IS_WAYLAND:
            try:
                result = subprocess.run(['wl-paste', '--no-newline'],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    return result.stdout
            except (FileNotFoundError, subprocess.SubprocessError):
                pass
        try:
            import pyperclip
            return pyperclip.paste()
        except Exception:
            return ""
