import collections
import ctypes
import ctypes.wintypes
import io
import os
import queue
import re
import sys
import threading
import time
import wave
from pathlib import Path

import groq
import numpy as np
import pyperclip
import pystray
import sounddevice as sd
from dotenv import load_dotenv
from PIL import Image, ImageDraw

try:
    import tkinter as tk
    from tkinter import messagebox
    _HAS_TK = True
except ImportError:
    _HAS_TK = False

try:
    import winreg
    _HAS_WINREG = True
except ImportError:
    _HAS_WINREG = False

_AUDIO_LOCK = threading.Lock()


def _refresh_audio() -> None:
    """Terminate and reinitialize PortAudio so newly connected devices are visible.

    Uses a lock so concurrent calls from the device-monitor thread and the
    tkinter thread cannot interleave sd._terminate() / sd._initialize() calls.
    """
    with _AUDIO_LOCK:
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            _log(f"PortAudio refresh error: {e}")


SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
WHISPER_MODEL_DEFAULT = "whisper-large-v3-turbo"

# ---- Logging ----

LOG_FILE = Path(__file__).parent / "voiceinput.log"
_LOG_MAX = 512 * 1024  # 512 KB before rotation
_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Append timestamped message to voiceinput.log and stderr.

    Using pythonw hides stderr, so the log file is the only way to see errors.
    """
    t = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
    line = f"[{t}] {msg}\n"
    print(line, end="", file=sys.stderr, flush=True)
    with _LOG_LOCK:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_MAX:
                bak = LOG_FILE.parent / "voiceinput.log.bak"
                LOG_FILE.replace(bak)
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass


WHISPER_PROMPT = "こんにちは。今日は、とても良い天気ですね。これから、音声入力を開始します。"
MIN_RECORDING_SECONDS = 0.5
PASTE_DELAY = 0.3
BLOCKSIZE = 4096

TRAY_COLORS = {
    "idle":       "#808080",
    "recording":  "#FF3333",
    "processing": "#FFB800",
    "error":      "#FF6600",
}

ENV_PATH = Path(__file__).parent / ".env"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "VoiceInput"


# ---- .env helpers ----

def _update_env(key: str, value: str) -> None:
    content = ENV_PATH.read_text("utf-8") if ENV_PATH.exists() else ""
    pattern = rf"^{re.escape(key)}=.*"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(content, "utf-8")


# ---- Startup (Windows registry) helpers ----

def _startup_cmd() -> str:
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    exe = str(pythonw) if pythonw.exists() else sys.executable
    return f'"{exe}" "{Path(__file__).resolve()}"'


def _is_startup_registered() -> bool:
    if not _HAS_WINREG:
        return False
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(k, STARTUP_REG_NAME)
        winreg.CloseKey(k)
        return True
    except OSError:
        return False


def _register_startup() -> None:
    if not _HAS_WINREG:
        return
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(k, STARTUP_REG_NAME, 0, winreg.REG_SZ, _startup_cmd())
    winreg.CloseKey(k)


def _unregister_startup() -> None:
    if not _HAS_WINREG:
        return
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, STARTUP_REG_NAME)
        winreg.CloseKey(k)
    except OSError:
        pass


# ---- SendInput for Ctrl+V paste (bypasses keyboard library entirely) ----
#
# keyboard.send("ctrl+v") routes synthetic events through our own
# WH_KEYBOARD_LL hook and corrupts the suppress-state-machine, silently
# disabling all subsequent hotkey detection.  SendInput is a lower-level
# Win32 API that injects directly into the input stream without touching
# any Python hook — completely safe.

_VK_CONTROL        = 0x11
_VK_V              = 0x56
_KEYEVENTF_KEYDOWN = 0x0000
_KEYEVENTF_KEYUP   = 0x0002
_INPUT_KEYBOARD    = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),   # ULONG_PTR — pointer-sized
    ]


class _MOUSEINPUT(ctypes.Structure):
    # Included so the union is sized correctly (MOUSEINPUT is larger than
    # KEYBDINPUT on 64-bit; the union must be max(sizeof(mi), sizeof(ki))).
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type",  ctypes.c_ulong),
        ("_data", _INPUT_UNION),
    ]


def _send_ctrl_v() -> None:
    """Inject Ctrl+V via Win32 SendInput; no Python keyboard hook involved."""
    inputs = (_INPUT * 4)()

    def _make(vk: int, flags: int) -> _INPUT:
        inp = _INPUT()
        inp.type = _INPUT_KEYBOARD
        inp._data.ki.wVk = vk
        inp._data.ki.dwFlags = flags
        return inp

    inputs[0] = _make(_VK_CONTROL, _KEYEVENTF_KEYDOWN)
    inputs[1] = _make(_VK_V,       _KEYEVENTF_KEYDOWN)
    inputs[2] = _make(_VK_V,       _KEYEVENTF_KEYUP)
    inputs[3] = _make(_VK_CONTROL, _KEYEVENTF_KEYUP)

    sent = ctypes.windll.user32.SendInput(4, inputs, ctypes.sizeof(_INPUT))
    if sent != 4:
        err = ctypes.windll.kernel32.GetLastError()
        _log(f"_send_ctrl_v: SendInput sent {sent}/4 events (GetLastError={err})")


# ---- Win32 RegisterHotKey listener ----

class HotkeyListener:
    """System-wide hotkey via Win32 RegisterHotKey + WM_HOTKEY message loop.

    Unlike WH_KEYBOARD_LL hooks (used by the `keyboard` library):
    - Events are delivered via the thread's message queue, not a hook callback
    - Never subject to LowLevelHooksTimeout removal by Windows
    - Not affected by IME or other apps installing their own hooks after us
    - No suppress-state-machine that can be corrupted by synthetic key events

    Usage:
        listener = HotkeyListener("ctrl+space", callback)
        listener.start()   # blocks briefly until RegisterHotKey completes
        # ... later ...
        listener.stop()    # posts WM_QUIT to the message loop thread
    """

    MOD_ALT      = 0x0001
    MOD_CONTROL  = 0x0002
    MOD_SHIFT    = 0x0004
    MOD_WIN      = 0x0008
    MOD_NOREPEAT = 0x4000  # Suppress auto-repeat while the key is held down

    WM_HOTKEY = 0x0312
    WM_QUIT   = 0x0012
    HOTKEY_ID = 1          # Arbitrary ID; unique per thread, not per process

    # Map hotkey string tokens → Windows virtual-key codes
    _VK: dict[str, int] = {
        "space":     0x20,
        "enter":     0x0D,
        "tab":       0x09,
        "escape":    0x1B,
        "esc":       0x1B,
        "backspace": 0x08,
        "delete":    0x2E,
        "del":       0x2E,
        "insert":    0x2D,
        "ins":       0x2D,
        "home":      0x24,
        "end":       0x23,
        "pageup":    0x21,
        "pgup":      0x21,
        "pagedown":  0x22,
        "pgdn":      0x22,
        "left":      0x25,
        "up":        0x26,
        "right":     0x27,
        "down":      0x28,
        **{f"f{i}": 0x6F + i for i in range(1, 13)},   # F1=0x70 … F12=0x7B
        **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
        **{str(i): 0x30 + i for i in range(10)},
    }

    # Map modifier tokens → Windows MOD_* flags
    _MOD_MAP: dict[str, int] = {
        "ctrl":    MOD_CONTROL,
        "control": MOD_CONTROL,
        "alt":     MOD_ALT,
        "shift":   MOD_SHIFT,
        "win":     MOD_WIN,
        "windows": MOD_WIN,
    }

    def __init__(self, hotkey_str: str, callback) -> None:
        self._hotkey_str = hotkey_str
        self._callback = callback
        self._mods, self._vk = self._parse(hotkey_str)
        self._thread: threading.Thread | None = None
        self._tid: int = 0          # Win32 thread ID of the message-loop thread
        self.registered: bool = False

    @classmethod
    def _parse(cls, hotkey_str: str) -> tuple[int, int]:
        """Parse 'ctrl+shift+f2' into (mods_flags, vk_code)."""
        parts = [p.strip().lower() for p in hotkey_str.split("+")]
        mods = 0
        vk = 0
        for part in parts:
            if part in cls._MOD_MAP:
                mods |= cls._MOD_MAP[part]
            elif part in cls._VK:
                if vk:
                    raise ValueError(
                        f"Multiple non-modifier keys in hotkey {hotkey_str!r}"
                    )
                vk = cls._VK[part]
            else:
                raise ValueError(
                    f"Unknown key token {part!r} in hotkey {hotkey_str!r}"
                )
        if not vk:
            raise ValueError(
                f"No non-modifier key found in hotkey {hotkey_str!r}"
            )
        mods |= cls.MOD_NOREPEAT
        return mods, vk

    def start(self) -> None:
        """Launch the message-loop thread; wait up to 2 s for registration."""
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(ready,), daemon=True, name="HotkeyListener"
        )
        self._thread.start()
        ready.wait(timeout=2.0)

    def stop(self) -> None:
        """Ask the message loop to exit cleanly (posts WM_QUIT)."""
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, ready: threading.Event) -> None:
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()

        ok = user32.RegisterHotKey(None, self.HOTKEY_ID, self._mods, self._vk)
        if not ok:
            err = kernel32.GetLastError()
            _log(
                f"HotkeyListener: RegisterHotKey FAILED "
                f"(mods=0x{self._mods:04X} vk=0x{self._vk:02X} error={err}) "
                f"— hotkey may already be claimed by another app"
            )
            ready.set()
            return

        self.registered = True
        _log(
            f"HotkeyListener: registered OK "
            f"(mods=0x{self._mods:04X} vk=0x{self._vk:02X})"
        )
        ready.set()

        msg = ctypes.wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                # 0 = WM_QUIT received; -1 = GetMessageW error
                break
            if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                try:
                    self._callback()
                except Exception as e:
                    _log(f"HotkeyListener: callback raised {type(e).__name__}: {e}")

        user32.UnregisterHotKey(None, self.HOTKEY_ID)
        self.registered = False
        self._tid = 0
        _log("HotkeyListener: message loop stopped")


# ---- Core classes ----

class AppState:
    def __init__(self):
        self.is_recording = False
        self.is_processing = False
        self.pending_record = False
        self.lock = threading.Lock()


class AudioRecorder:
    def __init__(self, device: int | None = None):
        self._device = device
        self._chunks: collections.deque = collections.deque()
        self._stream: sd.InputStream | None = None

    def start(self):
        # Close any orphaned stream left over from an aborted recording
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._chunks.clear()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self._callback,
            blocksize=BLOCKSIZE,
            device=self._device,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[Audio] {status}", file=sys.stderr)
        self._chunks.append(indata.copy())

    def stop(self) -> bytes:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        chunks = list(self._chunks)
        audio_data = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, CHANNELS), dtype=np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        return buf.getvalue()

    def estimate_duration(self) -> float:
        return len(self._chunks) * BLOCKSIZE / SAMPLE_RATE


class GroqTranscriber:
    def __init__(self, api_key: str, model: str = WHISPER_MODEL_DEFAULT):
        self._client = groq.Groq(api_key=api_key, timeout=30.0)
        self._model = model

    def transcribe(self, wav_bytes: bytes) -> str:
        response = self._client.audio.transcriptions.create(
            model=self._model,
            file=("recording.wav", wav_bytes, "audio/wav"),
            language="ja",
            prompt=WHISPER_PROMPT,
            response_format="text",
        )
        return response.strip() if isinstance(response, str) else response.text.strip()


def _set_foreground(hwnd: int) -> None:
    """Bring hwnd to foreground reliably using AttachThreadInput workaround.

    Plain SetForegroundWindow() is silently ignored on Windows 10/11 when called
    from a background thread that doesn't currently own the foreground lock.
    Attaching to the foreground thread's input queue first grants that right.
    """
    user32   = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    fg_hwnd  = user32.GetForegroundWindow()
    fg_tid   = user32.GetWindowThreadProcessId(fg_hwnd, None)
    cur_tid  = kernel32.GetCurrentThreadId()
    attached = fg_tid and fg_tid != cur_tid
    if attached:
        user32.AttachThreadInput(fg_tid, cur_tid, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if attached:
        user32.AttachThreadInput(fg_tid, cur_tid, False)


class PasteHandler:
    def paste_text(self, text: str, hwnd: int = 0) -> None:
        if not text:
            return
        pyperclip.copy(text)
        if hwnd:
            _set_foreground(hwnd)
        time.sleep(PASTE_DELAY)
        _send_ctrl_v()


# ---- Settings GUI (tkinter in background thread) ----

def _center(win: "tk.Toplevel") -> None:
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")


class SettingsManager:
    def __init__(self, app: "VoiceInputApp"):
        self._app = app
        self._q: queue.Queue = queue.Queue()
        self._root: "tk.Tk | None" = None
        if _HAS_TK:
            threading.Thread(target=self._run_tk, daemon=True).start()

    def _run_tk(self) -> None:
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.after(100, self._poll)
        self._root.mainloop()

    def _poll(self) -> None:
        try:
            while True:
                try:
                    self._q.get_nowait()()
                except queue.Empty:
                    break
                except Exception as e:
                    _log(f"SettingsManager callback error: {type(e).__name__}: {e}")
        except Exception:
            pass
        if self._root:
            self._root.after(100, self._poll)

    def _schedule(self, func) -> None:
        if _HAS_TK:
            self._q.put(func)

    # ---- tray menu entry points ----

    def show_new_mic_toast(self, names: str) -> None:
        self._schedule(lambda: self._do_new_mic_toast(names))

    def _do_new_mic_toast(self, names: str) -> None:
        if not self._root:
            return
        win = tk.Toplevel(self._root)
        win.title("Voice Input — 新しいマイク")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        tk.Label(win, text="新しいマイクが接続されました", font=("", 10, "bold"), padx=16, pady=10).pack()
        tk.Label(win, text=names, padx=16, pady=(0, 4)).pack()
        tk.Label(win, text="トレイ → Microphone... で選択できます", fg="gray", padx=16, pady=(0, 10)).pack()
        _center(win)
        win.after(6000, win.destroy)

    def show_mic_dialog(self, icon=None, item=None):
        self._schedule(self._do_mic)

    def show_hotkey_dialog(self, icon=None, item=None):
        self._schedule(self._do_hotkey)

    def show_startup_dialog(self, icon=None, item=None):
        self._schedule(self._do_startup)

    # ---- dialogs ----

    def _make_win(self, title: str) -> "tk.Toplevel":
        win = tk.Toplevel(self._root)
        win.title(title)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.grab_set()
        win.focus_force()
        return win

    def _do_mic(self) -> None:
        try:
            with _AUDIO_LOCK:
                devs = sd.query_devices()
            inputs = [(i, d["name"]) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
        except Exception as e:
            messagebox.showerror("Error", f"Cannot read audio devices:\n{e}")
            return

        win = self._make_win("Microphone Settings")
        tk.Label(win, text="Select microphone:", anchor="w", padx=16, pady=10).pack(fill="x")

        var = tk.IntVar(value=-1 if self._app._recorder._device is None else self._app._recorder._device)

        f = tk.Frame(win, padx=24)
        f.pack(fill="x", pady=(0, 8))
        tk.Radiobutton(f, text="System default", variable=var, value=-1).pack(anchor="w")
        for idx, name in inputs:
            tk.Radiobutton(f, text=f"[{idx}]  {name}", variable=var, value=idx).pack(anchor="w")

        bf = tk.Frame(win)
        bf.pack(pady=(4, 12))

        def ok():
            v = var.get()
            self._app._apply_mic(None if v == -1 else v)
            win.destroy()

        tk.Button(bf, text="OK", width=9, command=ok).pack(side="left", padx=4)
        tk.Button(bf, text="Cancel", width=9, command=win.destroy).pack(side="left", padx=4)
        _center(win)
        win.wait_window()

    def _do_hotkey(self) -> None:
        win = self._make_win("Hotkey Settings")
        tk.Label(win, text="New hotkey:", anchor="w", padx=16, pady=10).pack(fill="x")

        entry = tk.Entry(win, width=26)
        entry.insert(0, self._app._hotkey)
        entry.pack(padx=16, pady=(0, 4))
        tk.Label(win, text="e.g.  ctrl+space  /  ctrl+shift+f2", fg="gray", padx=16).pack(anchor="w")

        bf = tk.Frame(win)
        bf.pack(pady=(8, 12))

        def ok():
            h = entry.get().strip().lower()
            if not h:
                return
            try:
                self._app._apply_hotkey(h)
                win.destroy()
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=win)

        entry.bind("<Return>", lambda _: ok())
        tk.Button(bf, text="OK", width=9, command=ok).pack(side="left", padx=4)
        tk.Button(bf, text="Cancel", width=9, command=win.destroy).pack(side="left", padx=4)
        _center(win)
        entry.focus_set()
        entry.select_range(0, "end")
        win.wait_window()

    def _do_startup(self) -> None:
        win = self._make_win("Startup Settings")
        var = tk.BooleanVar(value=_is_startup_registered())

        tk.Checkbutton(
            win,
            text="Launch Voice Input when Windows starts",
            variable=var,
            padx=16, pady=14,
        ).pack(anchor="w")

        bf = tk.Frame(win)
        bf.pack(pady=(0, 12))

        def ok():
            _register_startup() if var.get() else _unregister_startup()
            win.destroy()

        tk.Button(bf, text="OK", width=9, command=ok).pack(side="left", padx=4)
        tk.Button(bf, text="Cancel", width=9, command=win.destroy).pack(side="left", padx=4)
        _center(win)
        win.wait_window()


# ---- Tray icon ----

def _make_icon(state_name: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=TRAY_COLORS.get(state_name, TRAY_COLORS["idle"]))
    return img


# ---- Application ----

class VoiceInputApp:
    def __init__(self):
        load_dotenv()
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            sys.exit("ERROR: GROQ_API_KEY not set. Copy .env.example to .env and add your key.")

        self._hotkey = os.environ.get("HOTKEY", "ctrl+space")
        model = os.environ.get("WHISPER_MODEL", WHISPER_MODEL_DEFAULT)

        mic_raw = os.environ.get("MICROPHONE_INDEX", "").strip()
        mic_index: int | None = None
        try:
            v = int(mic_raw)
            if v >= 0:
                mic_index = v
        except (ValueError, TypeError):
            pass

        self._state = AppState()
        self._recorder = AudioRecorder(device=mic_index)
        self._transcriber = GroqTranscriber(api_key=api_key, model=model)
        self._paste = PasteHandler()
        self._settings = SettingsManager(self)
        self._icon: pystray.Icon | None = None
        self._hotkey_listener: HotkeyListener | None = None
        self._hwnd: int = 0

    # ---- live settings application ----

    def _apply_mic(self, device: int | None) -> None:
        self._recorder._device = device
        _update_env("MICROPHONE_INDEX", "" if device is None else str(device))

    def _apply_hotkey(self, new_hotkey: str) -> None:
        """Stop the current HotkeyListener and start a new one for new_hotkey."""
        # Stop old listener first so it can unregister its hotkey before the
        # new listener tries to register (RegisterHotKey allows the same key
        # on different threads, but we want a clean state).
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
            if self._hotkey_listener._thread is not None:
                self._hotkey_listener._thread.join(timeout=1.0)
        listener = HotkeyListener(new_hotkey, self._on_hotkey)
        listener.start()
        if not listener.registered:
            raise RuntimeError(
                f"RegisterHotKey failed — {new_hotkey!r} may be in use by another app"
            )
        self._hotkey_listener = listener
        self._hotkey = new_hotkey
        _update_env("HOTKEY", new_hotkey)
        _log(f"Hotkey changed to {new_hotkey!r}")
        if self._icon:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()

    # ---- recording / transcription ----

    def _set_icon_state(self, state: str) -> None:
        if self._icon:
            self._icon.icon = _make_icon(state)
            self._icon.title = f"Voice Input [{state}]"

    def _on_hotkey(self) -> None:
        # Called from HotkeyListener's message-loop thread (not a hook callback).
        # Safe to take locks and spawn threads; no timeout constraints here.
        with self._state.lock:
            if self._state.is_processing:
                self._state.pending_record = True
                self._hwnd = ctypes.windll.user32.GetForegroundWindow()
                _log("Hotkey: queued (processing in progress)")
                return
            starting = not self._state.is_recording
            if starting:
                self._hwnd = ctypes.windll.user32.GetForegroundWindow()
                self._state.is_recording = True
                _log("Hotkey: START recording")
            else:
                self._state.is_recording = False
                self._state.is_processing = True
                _log("Hotkey: STOP recording → transcribe")

        if starting:
            threading.Thread(target=self._start_recording, daemon=True).start()
        else:
            threading.Thread(target=self._stop_and_transcribe, args=(self._hwnd,), daemon=True).start()

    def _start_recording(self) -> None:
        # Give immediate visual feedback before the slow InputStream.start() call.
        # Without this the icon stays gray for ~500 ms on the first press, causing
        # users to press again thinking the hotkey was missed — which immediately
        # stops the just-started recording.
        self._set_icon_state("recording")
        try:
            self._recorder.start()
            _log("Recording started")
        except Exception as e:
            _log(f"Recording start FAILED: {type(e).__name__}: {e}")
            with self._state.lock:
                self._state.is_recording = False
            self._set_icon_state("error")
            return
        # Guard against an immediate second hotkey press that already set
        # is_recording back to False before we could start the stream.
        with self._state.lock:
            if not self._state.is_recording:
                self._recorder.stop()
                self._set_icon_state("idle")
                return

    def _stop_and_transcribe(self, hwnd: int = 0) -> None:
        # Entire body is guarded by try/finally so is_processing is ALWAYS
        # reset to False, even if recorder.stop() or the API call throws.
        try:
            duration = self._recorder.estimate_duration()
            _log(f"Transcribe: duration={duration:.2f}s")
            wav_bytes = self._recorder.stop()
            self._set_icon_state("processing")

            if duration >= MIN_RECORDING_SECONDS:
                text = self._transcriber.transcribe(wav_bytes)
                _log(f"Transcribe: result={text!r}")
                if text:
                    self._paste.paste_text(text, hwnd)
            else:
                _log(f"Transcribe: skipped (too short)")
        except groq.APIError as e:
            _log(f"API error: {type(e).__name__}: {e}")
            self._set_icon_state("error")
            time.sleep(2.0)
        except Exception as e:
            _log(f"Error in transcribe: {type(e).__name__}: {e}")
            self._set_icon_state("error")
            time.sleep(2.0)
        finally:
            start_next = False
            with self._state.lock:
                self._state.is_processing = False
                if self._state.pending_record:
                    self._state.pending_record = False
                    self._state.is_recording = True
                    start_next = True
            if start_next:
                _log("Transcribe done: starting queued recording")
                threading.Thread(target=self._start_recording, daemon=True).start()
            else:
                self._set_icon_state("idle")

    # ---- device monitor ----

    def _device_monitor(self) -> None:
        """Poll for newly connected microphones every 5 s and notify the user."""
        INTERVAL = 5
        known: set[str] = set()
        _refresh_audio()
        try:
            known = {d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0}
        except Exception:
            pass

        while True:
            time.sleep(INTERVAL)
            # Skip refresh while recording to avoid disrupting the active stream
            with self._state.lock:
                if self._state.is_recording:
                    continue
            try:
                _refresh_audio()
                current = {d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0}
            except Exception as e:
                _log(f"Device monitor: query_devices error: {e}")
                continue

            added = current - known
            if added:
                names = "\n".join(added)
                _log(f"Device monitor: new mic detected: {', '.join(added)}")
                self._settings.show_new_mic_toast(names)
            if current != known:
                known = current

    # ---- watchdog ----

    def _watchdog(self) -> None:
        """Background thread: detects and recovers from two failure modes.

        1. Stuck is_processing — if the transcription thread hangs (e.g. half-open
           TCP connection to Groq), is_processing stays True forever and every
           hotkey press is silently queued but never acted on.  We force-reset
           after STUCK_TIMEOUT seconds.

        2. Dead HotkeyListener — if the message-loop thread exits unexpectedly or
           RegisterHotKey failed silently, we restart it.
        """
        INTERVAL      = 30    # seconds between watchdog ticks
        STUCK_TIMEOUT = 90    # seconds before declaring is_processing stuck
        proc_since: float | None = None

        while True:
            time.sleep(INTERVAL)

            # Snapshot state outside lock to keep the lock brief
            with self._state.lock:
                is_proc = self._state.is_processing
                is_rec  = self._state.is_recording
                pending = self._state.pending_record

            # ---- stuck is_processing guard ----
            if is_proc:
                if proc_since is None:
                    proc_since = time.monotonic()
                elif time.monotonic() - proc_since > STUCK_TIMEOUT:
                    _log(f"Watchdog: is_processing stuck >{STUCK_TIMEOUT}s — force-resetting state")
                    with self._state.lock:
                        self._state.is_processing = False
                        self._state.pending_record = False
                    proc_since = None
                    self._set_icon_state("idle")
            else:
                proc_since = None

            # ---- HotkeyListener health check ----
            hl         = self._hotkey_listener
            alive      = hl is not None and hl.is_alive()
            registered = hl is not None and hl.registered

            _log(
                f"Watchdog: listener_alive={alive} registered={registered} "
                f"rec={is_rec} proc={is_proc} pend={pending}"
            )

            if not alive or not registered:
                _log("Watchdog: HotkeyListener dead or unregistered — restarting")
                try:
                    if hl is not None:
                        hl.stop()
                    new_listener = HotkeyListener(self._hotkey, self._on_hotkey)
                    new_listener.start()
                    self._hotkey_listener = new_listener
                    _log(
                        f"Watchdog: HotkeyListener restarted "
                        f"(registered={new_listener.registered})"
                    )
                except Exception as e:
                    _log(f"Watchdog: HotkeyListener restart FAILED: {e}")

    # ---- tray menu ----

    def _build_menu(self) -> pystray.Menu:
        hotkey = self._hotkey
        return pystray.Menu(
            pystray.MenuItem("Voice Input", action=None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Hotkey: {hotkey}", action=None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Microphone...",  self._settings.show_mic_dialog),
            pystray.MenuItem("Hotkey...",      self._settings.show_hotkey_dialog),
            pystray.MenuItem("Startup...",     self._settings.show_startup_dialog),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _on_quit(self, icon: pystray.Icon, item) -> None:
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        icon.stop()

    # ---- entry point ----

    def run(self) -> None:
        _log(f"Voice Input starting — hotkey={self._hotkey!r} mic={self._recorder._device!r}")

        try:
            sd.query_devices(self._recorder._device, kind="input")
        except sd.PortAudioError as e:
            _log(f"FATAL: Microphone not found: {e}")
            sys.exit(f"ERROR: Microphone not found: {e}")

        try:
            listener = HotkeyListener(self._hotkey, self._on_hotkey)
            listener.start()
            if not listener.registered:
                raise RuntimeError(
                    f"RegisterHotKey failed — {self._hotkey!r} may be in use by another app"
                )
            self._hotkey_listener = listener
            _log("HotkeyListener started OK")
        except Exception as e:
            _log(f"FATAL: Failed to register hotkey: {e}")
            sys.exit(f"ERROR: Failed to register hotkey ({self._hotkey}): {e}")

        threading.Thread(target=self._watchdog, daemon=True, name="Watchdog").start()
        _log("Watchdog started")

        threading.Thread(target=self._device_monitor, daemon=True, name="DeviceMonitor").start()
        _log("DeviceMonitor started")

        self._icon = pystray.Icon(
            name="voiceinput",
            icon=_make_icon("idle"),
            title="Voice Input [idle]",
            menu=self._build_menu(),
        )
        self._icon.run()


if __name__ == "__main__":
    VoiceInputApp().run()
