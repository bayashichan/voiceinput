import collections
import io
import os
import sys
import threading
import time
import wave

import groq
import keyboard
import numpy as np
import pyperclip
import pystray
import sounddevice as sd
from dotenv import load_dotenv
from PIL import Image, ImageDraw

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
WHISPER_MODEL_DEFAULT = "whisper-large-v3-turbo"
WHISPER_PROMPT = "こんにちは。今日は、とても良い天気ですね。これから、音声入力を開始します。"
MIN_RECORDING_SECONDS = 0.5
PASTE_DELAY = 0.15
BLOCKSIZE = 4096

TRAY_COLORS = {
    "idle":       "#808080",
    "recording":  "#FF3333",
    "processing": "#FFB800",
    "error":      "#FF6600",
}


class AppState:
    def __init__(self):
        self.is_recording = False
        self.is_processing = False
        self.lock = threading.Lock()


class AudioRecorder:
    def __init__(self):
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
        if chunks:
            audio_data = np.concatenate(chunks, axis=0)
        else:
            audio_data = np.zeros((0, CHANNELS), dtype=np.int16)
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


class PasteHandler:
    def paste_text(self, text: str) -> None:
        if not text:
            return
        pyperclip.copy(text)
        time.sleep(PASTE_DELAY)
        keyboard.send("ctrl+v")


def _make_icon(state_name: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = TRAY_COLORS.get(state_name, TRAY_COLORS["idle"])
    draw.ellipse([4, 4, 60, 60], fill=color)
    return img


class VoiceInputApp:
    def __init__(self):
        load_dotenv()
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            sys.exit("ERROR: GROQ_API_KEY が設定されていません。.env.example を .env にコピーして API キーを設定してください。")

        self._hotkey = os.environ.get("HOTKEY", "ctrl+shift+space")
        model = os.environ.get("WHISPER_MODEL", WHISPER_MODEL_DEFAULT)

        self._state = AppState()
        self._recorder = AudioRecorder()
        self._transcriber = GroqTranscriber(api_key=api_key, model=model)
        self._paste_handler = PasteHandler()
        self._icon: pystray.Icon | None = None

    def _set_icon_state(self, state_name: str) -> None:
        if self._icon:
            self._icon.icon = _make_icon(state_name)
            self._icon.title = f"Voice Input [{state_name}]"

    def _on_hotkey(self) -> None:
        with self._state.lock:
            if self._state.is_processing:
                return
            if not self._state.is_recording:
                self._start_recording()
            else:
                self._state.is_recording = False
                self._state.is_processing = True
                threading.Thread(target=self._stop_and_transcribe, daemon=True).start()

    def _start_recording(self) -> None:
        self._state.is_recording = True
        self._recorder.start()
        self._set_icon_state("recording")
        print("[Voice Input] 録音開始")

    def _stop_and_transcribe(self) -> None:
        duration = self._recorder.estimate_duration()
        wav_bytes = self._recorder.stop()
        self._set_icon_state("processing")
        print(f"[Voice Input] 録音停止 ({duration:.1f}秒)")

        if duration < MIN_RECORDING_SECONDS:
            print("[Voice Input] 録音が短すぎます。スキップ。")
            with self._state.lock:
                self._state.is_processing = False
            self._set_icon_state("idle")
            return

        try:
            text = self._transcriber.transcribe(wav_bytes)
            print(f"[Voice Input] 認識結果: {text!r}")
            if text:
                self._paste_handler.paste_text(text)
            else:
                print("[Voice Input] 認識結果が空です。貼り付けをスキップ。")
        except groq.APIError as e:
            print(f"[Voice Input] Groq API エラー: {e}", file=sys.stderr)
            self._set_icon_state("error")
            time.sleep(2.0)
        except Exception as e:
            print(f"[Voice Input] 予期しないエラー: {e}", file=sys.stderr)
            self._set_icon_state("error")
            time.sleep(2.0)
        finally:
            with self._state.lock:
                self._state.is_processing = False
            self._set_icon_state("idle")

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Voice Input", action=None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"ホットキー: {self._hotkey}", action=None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", self._on_quit),
        )

    def _on_quit(self, icon: pystray.Icon, item) -> None:
        keyboard.unhook_all()
        icon.stop()

    def run(self) -> None:
        try:
            sd.query_devices(kind="input")
        except sd.PortAudioError as e:
            sys.exit(f"ERROR: マイクが見つかりません: {e}")

        try:
            keyboard.add_hotkey(self._hotkey, self._on_hotkey, suppress=True)
        except Exception as e:
            sys.exit(f"ERROR: ホットキーの登録に失敗しました ({self._hotkey}): {e}\n管理者として実行してみてください。")

        self._icon = pystray.Icon(
            name="voiceinput",
            icon=_make_icon("idle"),
            title="Voice Input [idle]",
            menu=self._build_menu(),
        )

        print(f"[Voice Input] 起動完了。ホットキー: {self._hotkey}")
        print("[Voice Input] タスクトレイのアイコンを右クリックして終了できます。")
        self._icon.run()


if __name__ == "__main__":
    app = VoiceInputApp()
    app.run()
