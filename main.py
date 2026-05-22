import collections
import ctypes
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
import keyboard
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

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
WHISPER_MODEL_DEFAULT = "whisper-large-v3-turbo"
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


# ---- Core classes ----

class AppState:
    def __init__(self):
        self.is_recording = False
        self.is_processing = False
        self.lock = threading.Lock()


class AudioRecorder:
    def __init__(self, device: int | None = None):
        self._device = device
        self._chunks: collections.deque = collections.deque()
        self._stream: sd.InputStream | None = None

    def start(self):
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
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    fg_hwnd = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
    cur_tid = kernel32.GetCurrentThreadId()
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
        keyboard.send("ctrl+v")


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
                self._q.get_nowait()()
        except queue.Empty:
            pass
        if self._root:
            self._root.after(100, self._poll)

    def _schedule(self, func) -> None:
        if _HAS_TK:
            self._q.put(func)

    # ---- tray menu entry points ----

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
        self._hotkey_handler = None
        self._hwnd: int = 0

    # ---- live settings application ----

    def _apply_mic(self, device: int | None) -> None:
        self._recorder._device = device
        _update_env("MICROPHONE_INDEX", "" if device is None else str(device))

    def _apply_hotkey(self, new_hotkey: str) -> None:
        if self._hotkey_handler is not None:
            keyboard.remove_hotkey(self._hotkey_handler)
        self._hotkey_handler = keyboard.add_hotkey(new_hotkey, self._on_hotkey, suppress=True)
        self._hotkey = new_hotkey
        _update_env("HOTKEY", new_hotkey)
        if self._icon:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()

    # ---- recording / transcription ----

    def _set_icon_state(self, state: str) -> None:
        if self._icon:
            self._icon.icon = _make_icon(state)
            self._icon.title = f"Voice Input [{state}]"

    def _on_hotkey(self) -> None:
        with self._state.lock:
            if self._state.is_processing:
                return
            starting = not self._state.is_recording
            if starting:
                # Capture target window NOW — user is guaranteed to be in it
                self._hwnd = ctypes.windll.user32.GetForegroundWindow()
                self._state.is_recording = True
            else:
                self._state.is_recording = False
                self._state.is_processing = True

        # Heavy operations run outside the lock so the hook callback returns fast
        if starting:
            try:
                self._recorder.start()
                self._set_icon_state("recording")
            except Exception as e:
                print(f"[Voice Input] Failed to start recording: {e}", file=sys.stderr)
                with self._state.lock:
                    self._state.is_recording = False
                self._set_icon_state("error")
        else:
            threading.Thread(target=self._stop_and_transcribe, args=(self._hwnd,), daemon=True).start()

    def _stop_and_transcribe(self, hwnd: int = 0) -> None:
        # Entire body is guarded by try/finally so is_processing is ALWAYS
        # reset to False, even if recorder.stop() or the API call throws.
        try:
            duration = self._recorder.estimate_duration()
            wav_bytes = self._recorder.stop()
            self._set_icon_state("processing")

            if duration >= MIN_RECORDING_SECONDS:
                text = self._transcriber.transcribe(wav_bytes)
                if text:
                    self._paste.paste_text(text, hwnd)
        except groq.APIError as e:
            print(f"[Voice Input] API error: {e}", file=sys.stderr)
            self._set_icon_state("error")
            time.sleep(2.0)
        except Exception as e:
            print(f"[Voice Input] Error: {e}", file=sys.stderr)
            self._set_icon_state("error")
            time.sleep(2.0)
        finally:
            with self._state.lock:
                self._state.is_processing = False
            self._set_icon_state("idle")

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
        keyboard.unhook_all()
        icon.stop()

    # ---- entry point ----

    def run(self) -> None:
        try:
            sd.query_devices(self._recorder._device, kind="input")
        except sd.PortAudioError as e:
            sys.exit(f"ERROR: Microphone not found: {e}")

        try:
            self._hotkey_handler = keyboard.add_hotkey(self._hotkey, self._on_hotkey, suppress=True)
        except Exception as e:
            sys.exit(f"ERROR: Failed to register hotkey ({self._hotkey}): {e}")

        self._icon = pystray.Icon(
            name="voiceinput",
            icon=_make_icon("idle"),
            title="Voice Input [idle]",
            menu=self._build_menu(),
        )
        self._icon.run()


if __name__ == "__main__":
    VoiceInputApp().run()
