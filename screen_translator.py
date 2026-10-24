import array
import base64
import ctypes
from ctypes import wintypes
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import wave
import webbrowser

from PIL import Image, ImageChops, ImageGrab, ImageStat
from google import genai

# Windows DPI 보정 (실제 모니터 픽셀과 캡처 좌표 일치)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

CONFIG_PATH = Path(__file__).with_name("translator_config.json")
USAGE_PATH = Path(__file__).with_name("token_usage.json")
BRIDGE_PORT = 8765

MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-4-argon",
]

LANGUAGES = {
    "한국어 (ko)": "한국어",
    "영어 (en)": "English",
    "일본어 (ja)": "일본어",
    "중국어 간체 (zh)": "중국어(간체)",
}

# Google AI Studio 기본 한도 프리셋 (사용자가 UI에서 변경 가능)
TIER_PRESETS = {
    "무료 티어 (Free Tier)": {"rpm": 15, "tpm": 250_000, "rpd": 1_500, "daily_tokens": 1_000_000},
    "유료 티어 Tier 1 (Pay-as-you-go)": {"rpm": 1_000, "tpm": 4_000_000, "rpd": 50_000, "daily_tokens": 10_000_000},
}


# =====================================================================
# 1. 화면 픽셀 변화 감지 & 이미지 인코딩
# =====================================================================
def encode_And_diff(img: Image.Image, prev_small: Image.Image | None, threshold: float = 3.5):
    """# ponytail: 32x32 저해상도 픽셀 평균 차이로 화면 변경 감지, 로컬 OCR이 필요할 때 교체"""
    small = img.resize((32, 32)).convert("L")
    changed = True
    if prev_small is not None:
        diff = ImageStat.Stat(ImageChops.difference(small, prev_small)).mean[0]
        changed = diff >= threshold
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode("ascii"), small, changed


# =====================================================================
# 2. Windows 창 탐색 및 상대 영역 추적 (ctypes 표준 라이브러리)
# =====================================================================
user32 = ctypes.windll.user32


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not user32.IsWindow(hwnd) or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_virtual_screen() -> tuple[int, int, int, int]:
    """모든 모니터를 합친 가상 데스크톱 (x, y, w, h). 보조 모니터가 왼쪽/위쪽이면 x, y가 음수"""
    return (
        user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
        user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
        user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
    )


def list_visible_windows(exclude_titles: tuple[str, ...] = ()) -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """현재 화면에 표시 중인 유효한 애플리케이션 창 목록 반환: [(hwnd, title, rect), ...]"""
    results: list[tuple[int, str, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or any(ex in title for ex in exclude_titles):
            return True
        rect = get_window_rect(hwnd)
        if rect and (rect[2] - rect[0] >= 80) and (rect[3] - rect[1] >= 40):
            results.append((int(hwnd), title, rect))
        return True

    user32.EnumWindows(enum_proc, 0)
    return results


def apply_click_through(hwnd: int, enable: bool):
    """번역 결과 자막 창에 마우스 클릭 투과(WS_EX_TRANSPARENT) 적용/해제"""
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enable:
        new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT
    else:
        new_style = style & ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)


# =====================================================================
# 3. 오디오 사용 프로그램 탐색 (pycaw) 및 루프백 녹음 (pyaudiowpatch)
# =====================================================================
def list_audio_sessions() -> list[dict]:
    """현재 오디오 세션이 존재하는 실행 중인 프로그램 목록과 실시간 소리 출력 피크(Peak) 반환"""
    sessions_info = []
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

        comtypes.CoInitialize()
        for s in AudioUtilities.GetAllSessions():
            if not s.Process:
                continue
            try:
                name = s.Process.name()
                pid = s.Process.pid
            except Exception:
                continue
            peak = 0.0
            try:
                meter = s._ctl.QueryInterface(IAudioMeterInformation)
                peak = float(meter.GetPeakValue())
            except Exception:
                pass
            sessions_info.append({
                "pid": pid,
                "name": name,
                "peak": peak,
                "active": peak > 0.005 or s.State == 1,
            })
    except Exception:
        pass
    # 소리 출력 중인 프로세스를 상단에 정렬
    sessions_info.sort(key=lambda x: (not x["active"], -x["peak"], x["name"].lower()))
    return sessions_info


def is_process_making_sound(target_name: str | None) -> bool:
    """특정 프로세스 이름이 지정된 경우, 해당 프로세스가 현재 오디오를 재생 중인지 확인"""
    if not target_name or target_name.startswith("전체 시스템"):
        return True
    for s in list_audio_sessions():
        if s["name"].lower() == target_name.lower() and (s["peak"] > 0.004 or s["active"]):
            return True
    return False


def record_wasapi_loopback_wav(duration_sec: float = 3.0) -> tuple[bytes | None, float]:
    """Windows WASAPI Loopback 장치에서 지정 시간(초) 동안 오디오를 캡처해 WAV 바이트와 RMS 볼륨을 반환"""
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        try:
            loopback = p.get_default_wasapi_loopback()
        except Exception:
            loopbacks = list(p.get_wasapi_loopback_device_info_generator())
            if not loopbacks:
                return None, 0.0
            loopback = loopbacks[0]

        rate = int(loopback["defaultSampleRate"])
        channels = max(1, int(loopback["maxInputChannels"]))
        chunk_size = 1024
        frames = []

        stream = p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=loopback["index"],
            frames_per_buffer=chunk_size,
        )
        num_chunks = max(1, int(rate / chunk_size * duration_sec))
        for _ in range(num_chunks):
            frames.append(stream.read(chunk_size, exception_on_overflow=False))
        stream.stop_stream()
        stream.close()

        raw = b"".join(frames)
        if not raw:
            return None, 0.0

        samples = array.array("h", raw)
        if channels > 1:
            # 모노 폴드다운으로 용량 및 토큰 절약
            mono = array.array("h", (samples[i] for i in range(0, len(samples), channels)))
        else:
            mono = samples

        if not mono:
            return None, 0.0
        sum_sq = sum(float(s) * float(s) for s in mono)
        rms = math.sqrt(sum_sq / len(mono)) / 32768.0

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(mono.tobytes())
        return buf.getvalue(), rms
    finally:
        p.terminate()


# =====================================================================
# 4. 크롬 탭별 단독 오디오 분리 브릿지 (Chrome Tab Audio Bridge)
# =====================================================================
BRIDGE_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Chrome 탭 단독 오디오 번역 브릿지</title>
<style>
  body { background:#1e1e2e; color:#cdd6f4; font-family:'Malgun Gothic',sans-serif; padding:24px; max-width:640px; margin:0 auto; }
  .card { background:#181825; border:1px solid #45475a; border-radius:10px; padding:20px; margin-top:16px; }
  button { background:#89b4fa; color:#11111b; border:none; padding:12px 20px; border-radius:6px; font-size:15px; font-weight:bold; cursor:pointer; }
  button.stop { background:#f38ba8; margin-left:8px; }
  .status { margin-top:14px; color:#a6e3a1; font-weight:bold; }
  .hint { color:#a6adc8; font-size:13px; line-height:1.6; }
  meter { width:100%; height:18px; margin-top:10px; }
</style>
</head>
<body>
  <h2>🌐 크롬 탭별 오디오 분리 캡처기</h2>
  <div class="card">
    <p class="hint">
      1. 아래 <b>[번역할 크롬 탭 선택하기]</b> 버튼을 클릭하세요.<br>
      2. 상단에서 <b>[Chrome 탭]</b>을 고른 뒤, 하단의 <b>"탭 오디오도 공유"</b>가 체크된 상태에서 원하는 탭을 선택하세요.<br>
      3. 다른 크롬 탭이나 PC 소리와 섞이지 않고 <b>선택한 탭의 소리만</b> 번역기로 실시간 전송됩니다.
    </p>
    <button id="btnStart">1. 번역할 크롬 탭 선택하기</button>
    <button id="btnStop" class="stop" disabled>중지</button>
    <div id="status" class="status">대기 중... 탭을 선택해 주세요.</div>
    <meter id="meter" min="0" max="0.25" value="0"></meter>
  </div>
<script>
let mediaStream = null, audioCtx = null, processor = null, chunkSec = 3.0;
const statusEl = document.getElementById('status');
const meterEl = document.getElementById('meter');

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (off, s) => { for (let i=0;i<s.length;i++) view.setUint8(off+i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, 'WAVE'); writeStr(12, 'fmt '); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  writeStr(36, 'data'); view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    let s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

document.getElementById('btnStart').onclick = async () => {
  try {
    mediaStream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    });
    const audioTracks = mediaStream.getAudioTracks();
    if (!audioTracks.length) {
      statusEl.textContent = '⚠️ 오디오 트랙이 없습니다! 탭 선택 시 "탭 오디오도 공유"를 체크해 주세요.';
      mediaStream.getTracks().forEach(t => t.stop());
      return;
    }
    const label = audioTracks[0].label || '선택된 크롬 탭';
    statusEl.textContent = '🔊 캡처 중: ' + label;
    document.getElementById('btnStart').disabled = true;
    document.getElementById('btnStop').disabled = false;

    audioCtx = new AudioContext({ sampleRate: 16000 });
    const src = audioCtx.createMediaStreamSource(new MediaStream([audioTracks[0]]));
    processor = audioCtx.createScriptProcessor(4096, 1, 1);
    let acc = [];
    processor.onaudioprocess = (e) => {
      const ch = e.inputBuffer.getChannelData(0);
      let sum = 0;
      for (let i=0; i<ch.length; i++) { sum += ch[i]*ch[i]; acc.push(ch[i]); }
      const rms = Math.sqrt(sum / ch.length);
      meterEl.value = Math.min(0.25, rms);
      if (acc.length >= 16000 * chunkSec) {
        const slice = Float32Array.from(acc);
        acc = [];
        let totalSq = 0;
        for (let i=0; i<slice.length; i++) totalSq += slice[i]*slice[i];
        const chunkRms = Math.sqrt(totalSq / slice.length);
        if (chunkRms > 0.006) {
          const wavBlob = encodeWav(slice, 16000);
          const reader = new FileReader();
          reader.onloadend = () => {
            const b64 = reader.result.split(',')[1];
            fetch('/audio_chunk', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ label: label, wav_b64: b64, rms: chunkRms })
            }).catch(()=>{});
          };
          reader.readAsDataURL(wavBlob);
        }
      }
    };
    src.connect(processor);
    processor.connect(audioCtx.destination);
  } catch (err) {
    statusEl.textContent = '취소 또는 오류: ' + err.message;
  }
};

document.getElementById('btnStop').onclick = () => {
  if (processor) processor.disconnect();
  if (audioCtx) audioCtx.close();
  if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
  document.getElementById('btnStart').disabled = false;
  document.getElementById('btnStop').disabled = true;
  statusEl.textContent = '중지됨.';
};
</script>
</body>
</html>
"""


class ChromeTabAudioBridge:
    def __init__(self, on_wav_chunk_callback, port: int = BRIDGE_PORT):
        self.on_wav_chunk = on_wav_chunk_callback
        self.port = port
        self.server: HTTPServer | None = None
        self.last_tab_label: str = "연결된 탭 없음"

    def start(self):
        if self.server is not None:
            return
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                body = BRIDGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path == "/audio_chunk":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length)
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                        bridge.last_tab_label = payload.get("label", "Chrome Tab")
                        wav_b64 = payload.get("wav_b64", "")
                        if wav_b64 and bridge.on_wav_chunk:
                            bridge.on_wav_chunk(bridge.last_tab_label, wav_b64)
                    except Exception:
                        pass
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
                else:
                    self.send_response(404)
                    self.end_headers()

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# =====================================================================
# 5. 토큰 및 무료/유료 할당량(RPM/TPM/RPD) 잔여량 트래커
# =====================================================================
class TokenQuotaTracker:
    def __init__(self, storage_path: Path = USAGE_PATH):
        self.storage_path = storage_path
        self.lock = threading.Lock()
        self.minute_events: list[tuple[float, int]] = []  # (timestamp, tokens)
        self.day_str = datetime.date.today().isoformat()
        self.daily_requests = 0
        self.daily_input_tokens = 0
        self.daily_output_tokens = 0
        self.daily_total_tokens = 0
        self.last_request_tokens = 0
        self.load()

    def load(self):
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                if data.get("day_str") == datetime.date.today().isoformat():
                    self.daily_requests = int(data.get("daily_requests", 0))
                    self.daily_input_tokens = int(data.get("daily_input_tokens", 0))
                    self.daily_output_tokens = int(data.get("daily_output_tokens", 0))
                    self.daily_total_tokens = int(data.get("daily_total_tokens", 0))
            except Exception:
                pass

    def save(self):
        try:
            self.storage_path.write_text(
                json.dumps(
                    {
                        "day_str": self.day_str,
                        "daily_requests": self.daily_requests,
                        "daily_input_tokens": self.daily_input_tokens,
                        "daily_output_tokens": self.daily_output_tokens,
                        "daily_total_tokens": self.daily_total_tokens,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _prune_minute(self, now: float):
        cutoff = now - 60.0
        self.minute_events = [(t, tok) for (t, tok) in self.minute_events if t >= cutoff]

    def record(self, in_tok: int, out_tok: int, total_tok: int):
        now = time.time()
        today = datetime.date.today().isoformat()
        with self.lock:
            if today != self.day_str:
                self.day_str = today
                self.daily_requests = 0
                self.daily_input_tokens = 0
                self.daily_output_tokens = 0
                self.daily_total_tokens = 0
            tot = total_tok or (in_tok + out_tok)
            self.daily_requests += 1
            self.daily_input_tokens += in_tok
            self.daily_output_tokens += out_tok
            self.daily_total_tokens += tot
            self.last_request_tokens = tot
            self.minute_events.append((now, tot))
            self._prune_minute(now)
            self.save()

    def reset(self):
        with self.lock:
            self.minute_events.clear()
            self.daily_requests = 0
            self.daily_input_tokens = 0
            self.daily_output_tokens = 0
            self.daily_total_tokens = 0
            self.last_request_tokens = 0
            self.save()

    def snapshot(self, rpm_limit: int, tpm_limit: int, rpd_limit: int, daily_token_budget: int) -> dict:
        now = time.time()
        with self.lock:
            self._prune_minute(now)
            used_rpm = len(self.minute_events)
            used_tpm = sum(tok for _, tok in self.minute_events)
            rem_rpm = max(0, rpm_limit - used_rpm)
            rem_tpm = max(0, tpm_limit - used_tpm)
            rem_rpd = max(0, rpd_limit - self.daily_requests)
            rem_daily_tokens = max(0, daily_token_budget - self.daily_total_tokens)
            pct_remaining = (rem_daily_tokens / daily_token_budget * 100.0) if daily_token_budget > 0 else 0.0
            return {
                "used_rpm": used_rpm,
                "rem_rpm": rem_rpm,
                "used_tpm": used_tpm,
                "rem_tpm": rem_tpm,
                "used_rpd": self.daily_requests,
                "rem_rpd": rem_rpd,
                "daily_input": self.daily_input_tokens,
                "daily_output": self.daily_output_tokens,
                "daily_total": self.daily_total_tokens,
                "rem_daily_tokens": rem_daily_tokens,
                "pct_remaining": pct_remaining,
                "last_tokens": self.last_request_tokens,
            }


# =====================================================================
# 6. 분리형 번역 결과 창 (SubtitleOverlayWindow)
# =====================================================================
class SubtitleOverlayWindow:
    def __init__(self, master: tk.Tk):
        self.win = tk.Toplevel(master)
        self.win.title("번역 결과 창 (자막 오버레이)")
        self.win.geometry("680x220+120+580")
        self.win.configure(bg="#11111b")
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.88)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        # 상단 드래그 핸들 바
        self.header = tk.Frame(self.win, bg="#181825", height=26, cursor="fleur")
        self.header.pack(fill="x", side="top")
        self.lbl_info = tk.Label(
            self.header,
            text=" 💬 실시간 번역 결과 창 (드래그하여 이동 / 모서리로 크기 조절)",
            fg="#89b4fa",
            bg="#181825",
            font=("Malgun Gothic", 9, "bold"),
        )
        self.lbl_info.pack(side="left", padx=6, pady=2)

        self.drag_offset = (0, 0)
        for w in (self.header, self.lbl_info):
            w.bind("<ButtonPress-1>", self._start_move)
            w.bind("<B1-Motion>", self._on_move)

        # 본문: 화면 번역 영역 / 오디오 번역 영역
        self.text = tk.Text(
            self.win,
            wrap="word",
            font=("Malgun Gothic", 14, "bold"),
            bg="#11111b",
            fg="#cdd6f4",
            relief="flat",
            padx=12,
            pady=8,
            insertbackground="#cdd6f4",
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("screen_tag", foreground="#89dceb")
        self.text.tag_configure("audio_tag", foreground="#a6e3a1")
        self.text.tag_configure("body_tag", foreground="#f5e0dc")

        # 우측 하단 크기 조절 그립
        grip = ttk.Sizegrip(self.win)
        grip.place(relx=1.0, rely=1.0, anchor="se")

        self.screen_text = ""
        self.audio_text = ""

    def _start_move(self, e):
        self.drag_offset = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _on_move(self, e):
        nx = e.x_root - self.drag_offset[0]
        ny = e.y_root - self.drag_offset[1]
        self.win.geometry(f"+{nx}+{ny}")

    def set_alpha(self, alpha: float):
        self.win.attributes("-alpha", max(0.15, min(1.0, float(alpha))))

    def set_font_size(self, size: int):
        self.text.configure(font=("Malgun Gothic", int(size), "bold"))

    def set_topmost(self, topmost: bool):
        self.win.attributes("-topmost", bool(topmost))

    def set_click_through(self, enabled: bool):
        self.win.update_idletasks()
        hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        apply_click_through(hwnd, enabled)

    def reset_geometry(self):
        self.win.deiconify()
        self.win.geometry("680x220+120+580")

    def show(self):
        self.win.deiconify()

    def hide(self):
        self.win.withdraw()

    def update_content(self, source: str, content: str, append: bool = False):
        if source == "screen":
            self.screen_text = (self.screen_text + content) if append else content
        else:
            self.audio_text = (self.audio_text + content) if append else content
        self._render()

    def _render(self):
        self.text.delete("1.0", "end")
        if self.screen_text:
            self.text.insert("end", "[🖥️ 화면 번역]\n", "screen_tag")
            self.text.insert("end", self.screen_text.strip() + "\n\n", "body_tag")
        if self.audio_text:
            self.text.insert("end", "[🔊 오디오 번역]\n", "audio_tag")
            self.text.insert("end", self.audio_text.strip() + "\n", "body_tag")
        self.text.see("end")


# =====================================================================
# 7. 메인 제어판 창 (ScreenTranslator / ControlPanel)
# =====================================================================
class ScreenTranslator:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gemini 3.8 Flash 실시간 번역기 - 제어판 (Control Panel)")
        self.root.geometry("620x720+80+40")
        self.root.configure(bg="#1e1e2e")

        self.quota = TokenQuotaTracker()
        self.overlay = SubtitleOverlayWindow(self.root)
        self.chrome_bridge = ChromeTabAudioBridge(self._on_chrome_tab_wav)

        # 상태 변수
        self.screen_on = False
        self.audio_on = False
        self.screen_busy = False
        self.audio_busy = False
        self.prev_small = None

        # 캡처 영역 (절대 좌표 또는 특정 창 상대 좌표)
        self.abs_bbox: tuple[int, int, int, int] | None = None
        self.target_hwnd: int | None = None
        self.rel_bbox: tuple[int, int, int, int] | None = None
        self.window_list: list[tuple[int, str, tuple[int, int, int, int]]] = []

        self._build_ui()
        self._load_config()
        self.refresh_windows()
        self.refresh_audio_sessions()
        self._refresh_quota_ui()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        pad = {"padx": 10, "pady": 5}

        # --- 1) Google AI Studio 및 모델 설정 ---
        frm_api = tk.LabelFrame(
            self.root, text=" 1. Google AI Studio & 모델 설정 ", bg="#1e1e2e", fg="#89b4fa", font=("Malgun Gothic", 10, "bold")
        )
        frm_api.pack(fill="x", **pad)

        r1 = tk.Frame(frm_api, bg="#1e1e2e")
        r1.pack(fill="x", padx=8, pady=4)
        tk.Label(r1, text="API Key:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left")
        self.api_var = tk.StringVar(value=os.environ.get("GEMINI_API_KEY", ""))
        tk.Entry(r1, textvariable=self.api_var, show="*", width=28, bg="#313244", fg="#cdd6f4", insertbackground="white").pack(
            side="left", padx=6
        )
        tk.Button(r1, text="설정 저장", command=self._save_config, bg="#a6e3a1", fg="#11111b", relief="flat").pack(side="left", padx=4)

        r2 = tk.Frame(frm_api, bg="#1e1e2e")
        r2.pack(fill="x", padx=8, pady=4)
        tk.Label(r2, text="번역 모델:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left")
        self.model_var = tk.StringVar(value="gemini-3.8-flash")
        ttk.Combobox(r2, textvariable=self.model_var, values=MODELS, width=22).pack(side="left", padx=6)

        tk.Label(r2, text="목표 언어:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left", padx=(10, 0))
        self.lang_var = tk.StringVar(value="한국어 (ko)")
        ttk.Combobox(r2, textvariable=self.lang_var, values=list(LANGUAGES.keys()), state="readonly", width=14).pack(
            side="left", padx=6
        )

        # --- 2) 화면 텍스트 번역 설정 ---
        frm_scr = tk.LabelFrame(
            self.root, text=" 2. 화면 / 애플리케이션 텍스트 번역 ", bg="#1e1e2e", fg="#89dceb", font=("Malgun Gothic", 10, "bold")
        )
        frm_scr.pack(fill="x", **pad)

        rs1 = tk.Frame(frm_scr, bg="#1e1e2e")
        rs1.pack(fill="x", padx=8, pady=4)
        self.btn_scr_toggle = tk.Button(
            rs1, text="화면 번역: OFF", width=16, command=self.toggle_screen, bg="#45475a", fg="#cdd6f4", relief="flat", font=("Malgun Gothic", 9, "bold")
        )
        self.btn_scr_toggle.pack(side="left")

        self.scr_mode_var = tk.StringVar(value="display")
        tk.Radiobutton(
            rs1, text="전체 디스플레이 영역", variable=self.scr_mode_var, value="display", bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244"
        ).pack(side="left", padx=8)
        tk.Radiobutton(
            rs1, text="지정 애플리케이션 창", variable=self.scr_mode_var, value="window", bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244"
        ).pack(side="left", padx=4)

        rs2 = tk.Frame(frm_scr, bg="#1e1e2e")
        rs2.pack(fill="x", padx=8, pady=4)
        tk.Label(rs2, text="대상 창:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left")
        self.win_combo_var = tk.StringVar()
        self.win_combo = ttk.Combobox(rs2, textvariable=self.win_combo_var, state="readonly", width=36)
        self.win_combo.pack(side="left", padx=6)
        self.win_combo.bind("<<ComboboxSelected>>", self._on_window_selected)
        tk.Button(rs2, text="창 새로고침", command=self.refresh_windows, bg="#585b70", fg="white", relief="flat").pack(side="left", padx=3)

        rs3 = tk.Frame(frm_scr, bg="#1e1e2e")
        rs3.pack(fill="x", padx=8, pady=4)
        tk.Button(rs3, text="📐 영역 드래그 지정", command=self.select_region, bg="#89b4fa", fg="#11111b", relief="flat").pack(
            side="left", padx=2
        )
        tk.Button(rs3, text="⚡ 1회 즉시 번역", command=lambda: self.trigger_screen_once(force=True), bg="#f9e2af", fg="#11111b", relief="flat").pack(
            side="left", padx=6
        )
        tk.Label(rs3, text="주기(초):", bg="#1e1e2e", fg="#cdd6f4").pack(side="left", padx=(8, 2))
        self.scr_interval_var = tk.DoubleVar(value=1.5)
        tk.Spinbox(rs3, from_=0.8, to=10.0, increment=0.5, textvariable=self.scr_interval_var, width=5).pack(side="left")

        self.lbl_scr_status = tk.Label(frm_scr, text="영역 미지정 (영역 드래그 지정 또는 대상 창을 선택하세요)", bg="#1e1e2e", fg="#a6adc8", anchor="w")
        self.lbl_scr_status.pack(fill="x", padx=8, pady=(0, 4))

        # --- 3) 오디오 사용 프로그램 & 크롬 탭 번역 설정 ---
        frm_aud = tk.LabelFrame(
            self.root, text=" 3. 오디오 사용 프로그램 / 크롬 탭별 실시간 번역 ", bg="#1e1e2e", fg="#a6e3a1", font=("Malgun Gothic", 10, "bold")
        )
        frm_aud.pack(fill="x", **pad)

        ra1 = tk.Frame(frm_aud, bg="#1e1e2e")
        ra1.pack(fill="x", padx=8, pady=4)
        self.btn_aud_toggle = tk.Button(
            ra1, text="오디오 번역: OFF", width=16, command=self.toggle_audio, bg="#45475a", fg="#cdd6f4", relief="flat", font=("Malgun Gothic", 9, "bold")
        )
        self.btn_aud_toggle.pack(side="left")

        self.aud_mode_var = tk.StringVar(value="process")
        tk.Radiobutton(
            ra1, text="실행 중인 프로그램 오디오", variable=self.aud_mode_var, value="process", bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244"
        ).pack(side="left", padx=8)
        tk.Radiobutton(
            ra1, text="크롬 탭별 단독 오디오 분리", variable=self.aud_mode_var, value="chrome_tab", bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244"
        ).pack(side="left", padx=4)

        ra2 = tk.Frame(frm_aud, bg="#1e1e2e")
        ra2.pack(fill="x", padx=8, pady=4)
        tk.Label(ra2, text="오디오 앱:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left")
        self.aud_combo_var = tk.StringVar()
        self.aud_combo = ttk.Combobox(ra2, textvariable=self.aud_combo_var, state="readonly", width=34)
        self.aud_combo.pack(side="left", padx=6)
        tk.Button(ra2, text="오디오 앱 새로고침", command=self.refresh_audio_sessions, bg="#585b70", fg="white", relief="flat").pack(
            side="left", padx=3
        )

        ra3 = tk.Frame(frm_aud, bg="#1e1e2e")
        ra3.pack(fill="x", padx=8, pady=4)
        tk.Button(
            ra3, text="🌐 크롬 탭 오디오 선택 창 열기", command=self.open_chrome_tab_selector, bg="#cba6f7", fg="#11111b", relief="flat"
        ).pack(side="left", padx=2)
        tk.Label(ra3, text="캡처 단위(초):", bg="#1e1e2e", fg="#cdd6f4").pack(side="left", padx=(12, 2))
        self.aud_chunk_var = tk.DoubleVar(value=3.0)
        tk.Spinbox(ra3, from_=2.0, to=8.0, increment=0.5, textvariable=self.aud_chunk_var, width=5).pack(side="left")

        self.lbl_aud_status = tk.Label(frm_aud, text="오디오 번역 대기 중", bg="#1e1e2e", fg="#a6adc8", anchor="w")
        self.lbl_aud_status.pack(fill="x", padx=8, pady=(0, 4))

        # --- 4) 번역 결과(자막) 창 스타일 및 투명도 설정 ---
        frm_ov = tk.LabelFrame(
            self.root, text=" 4. 번역 결과(자막) 창 투명도 · 위치 · 크기 제어 ", bg="#1e1e2e", fg="#f9e2af", font=("Malgun Gothic", 10, "bold")
        )
        frm_ov.pack(fill="x", **pad)

        ro1 = tk.Frame(frm_ov, bg="#1e1e2e")
        ro1.pack(fill="x", padx=8, pady=4)
        tk.Button(ro1, text="자막 창 보이기", command=self.overlay.show, bg="#89b4fa", fg="#11111b", relief="flat").pack(side="left", padx=2)
        tk.Button(ro1, text="자막 창 숨기기", command=self.overlay.hide, bg="#585b70", fg="white", relief="flat").pack(side="left", padx=4)
        tk.Button(ro1, text="위치/크기 초기화", command=self.overlay.reset_geometry, bg="#585b70", fg="white", relief="flat").pack(
            side="left", padx=4
        )

        self.topmost_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            ro1,
            text="항상 위",
            variable=self.topmost_var,
            command=lambda: self.overlay.set_topmost(self.topmost_var.get()),
            bg="#1e1e2e",
            fg="#cdd6f4",
            selectcolor="#313244",
        ).pack(side="left", padx=8)

        self.click_through_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            ro1,
            text="마우스 클릭 투과",
            variable=self.click_through_var,
            command=lambda: self.overlay.set_click_through(self.click_through_var.get()),
            bg="#1e1e2e",
            fg="#cdd6f4",
            selectcolor="#313244",
        ).pack(side="left", padx=4)

        ro2 = tk.Frame(frm_ov, bg="#1e1e2e")
        ro2.pack(fill="x", padx=8, pady=4)
        tk.Label(ro2, text="배경 투명도:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left")
        self.alpha_var = tk.DoubleVar(value=0.88)
        tk.Scale(
            ro2,
            from_=0.15,
            to=1.0,
            resolution=0.05,
            orient="horizontal",
            variable=self.alpha_var,
            command=lambda v: self.overlay.set_alpha(float(v)),
            bg="#1e1e2e",
            fg="#cdd6f4",
            highlightthickness=0,
            length=160,
        ).pack(side="left", padx=6)

        tk.Label(ro2, text="글자 크기:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left", padx=(10, 0))
        self.font_size_var = tk.IntVar(value=14)
        tk.Scale(
            ro2,
            from_=10,
            to=30,
            resolution=1,
            orient="horizontal",
            variable=self.font_size_var,
            command=lambda v: self.overlay.set_font_size(int(v)),
            bg="#1e1e2e",
            fg="#cdd6f4",
            highlightthickness=0,
            length=130,
        ).pack(side="left", padx=6)

        # --- 5) 토큰 및 할당량 잔여량 대시보드 ---
        frm_qt = tk.LabelFrame(
            self.root, text=" 5. 토큰 및 Google AI Studio 할당량 잔여량 ", bg="#1e1e2e", fg="#cba6f7", font=("Malgun Gothic", 10, "bold")
        )
        frm_qt.pack(fill="both", expand=True, **pad)

        rq1 = tk.Frame(frm_qt, bg="#1e1e2e")
        rq1.pack(fill="x", padx=8, pady=4)
        tk.Label(rq1, text="요금제 기준:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left")
        self.tier_var = tk.StringVar(value="무료 티어 (Free Tier)")
        tier_cb = ttk.Combobox(rq1, textvariable=self.tier_var, values=list(TIER_PRESETS.keys()), state="readonly", width=24)
        tier_cb.pack(side="left", padx=6)
        tier_cb.bind("<<ComboboxSelected>>", self._on_tier_changed)

        tk.Label(rq1, text="일일 토큰 예산:", bg="#1e1e2e", fg="#cdd6f4").pack(side="left", padx=(8, 2))
        self.budget_var = tk.IntVar(value=1_000_000)
        tk.Entry(rq1, textvariable=self.budget_var, width=10, bg="#313244", fg="#cdd6f4", insertbackground="white").pack(side="left")
        tk.Button(rq1, text="사용량 초기화", command=self._reset_quota, bg="#f38ba8", fg="#11111b", relief="flat").pack(side="right", padx=4)

        rq2 = tk.Frame(frm_qt, bg="#1e1e2e")
        rq2.pack(fill="x", padx=8, pady=4)
        self.progress = ttk.Progressbar(rq2, orient="horizontal", mode="determinate", maximum=100.0, value=100.0)
        self.progress.pack(fill="x")

        self.lbl_quota_main = tk.Label(
            frm_qt, text="잔여 토큰 계산 중...", bg="#1e1e2e", fg="#a6e3a1", font=("Malgun Gothic", 10, "bold"), anchor="w"
        )
        self.lbl_quota_main.pack(fill="x", padx=8, pady=2)

        self.lbl_quota_sub = tk.Label(frm_qt, text="", bg="#1e1e2e", fg="#cdd6f4", justify="left", anchor="w")
        self.lbl_quota_sub.pack(fill="x", padx=8, pady=2)

    # -----------------------------------------------------------------
    # 설정 저장/불러오기
    # -----------------------------------------------------------------
    def _load_config(self):
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if cfg.get("api_key"):
                    self.api_var.set(cfg["api_key"])
                if cfg.get("model"):
                    self.model_var.set(cfg["model"])
                if cfg.get("lang") in LANGUAGES:
                    self.lang_var.set(cfg["lang"])
                if cfg.get("daily_budget"):
                    self.budget_var.set(int(cfg["daily_budget"]))
            except Exception:
                pass

    def _save_config(self):
        try:
            CONFIG_PATH.write_text(
                json.dumps(
                    {
                        "api_key": self.api_var.get().strip(),
                        "model": self.model_var.get().strip(),
                        "lang": self.lang_var.get(),
                        "daily_budget": int(self.budget_var.get()),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.lbl_scr_status.config(text="✅ 설정이 translator_config.json에 저장되었습니다.")
        except Exception as e:
            self.lbl_scr_status.config(text=f"설정 저장 오류: {e}")

    # -----------------------------------------------------------------
    # 창 목록 및 영역 지정
    # -----------------------------------------------------------------
    def refresh_windows(self):
        exclude = ("Gemini 3.8 Flash 실시간 번역기", "번역 결과 창")
        self.window_list = list_visible_windows(exclude_titles=exclude)
        items = [f"[{hwnd}] {title}" for hwnd, title, _ in self.window_list]
        self.win_combo["values"] = items
        if items and not self.win_combo_var.get():
            self.win_combo.current(0)
            self._on_window_selected()

    def _on_window_selected(self, _event=None):
        idx = self.win_combo.current()
        if 0 <= idx < len(self.window_list):
            hwnd, title, rect = self.window_list[idx]
            self.target_hwnd = hwnd
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            self.rel_bbox = (0, 0, w, h)
            self.prev_small = None
            self.lbl_scr_status.config(text=f"선택 창: {title} ({w}x{h}) — 필요 시 [영역 드래그 지정]으로 창 일부만 선택 가능")

    def select_region(self):
        # 다중 디스플레이: -fullscreen은 한 모니터만 덮으므로 가상 데스크톱 전체를 덮는 테두리 없는 창 사용
        vx, vy, vw, vh = get_virtual_screen()
        sel = tk.Toplevel(self.root)
        sel.overrideredirect(True)
        sel.geometry(f"{vw}x{vh}+{vx}+{vy}")
        sel.attributes("-alpha", 0.3)
        sel.attributes("-topmost", True)
        sel.config(cursor="cross")
        canvas = tk.Canvas(sel, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        sel.focus_force()

        state = {"x0": 0, "y0": 0, "rect": None}

        def on_down(e):
            state["x0"], state["y0"] = e.x_root, e.y_root
            state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#89dceb", width=2)

        def on_drag(e):
            if state["rect"]:
                # 캔버스 좌표 = 화면 절대좌표 - 가상화면 원점
                canvas.coords(state["rect"], state["x0"] - vx, state["y0"] - vy, e.x, e.y)

        def on_up(e):
            x1, x2 = sorted([state["x0"], e.x_root])
            y1, y2 = sorted([state["y0"], e.y_root])
            sel.destroy()
            if x2 - x1 > 10 and y2 - y1 > 10:
                self.prev_small = None
                if self.scr_mode_var.get() == "window" and self.target_hwnd:
                    wrect = get_window_rect(self.target_hwnd)
                    if wrect:
                        self.rel_bbox = (x1 - wrect[0], y1 - wrect[1], x2 - wrect[0], y2 - wrect[1])
                        self.lbl_scr_status.config(text=f"창 기준 상대 영역 설정됨: {self.rel_bbox}")
                        return
                self.abs_bbox = (x1, y1, x2, y2)
                self.lbl_scr_status.config(text=f"디스플레이 영역 설정됨: {self.abs_bbox}")

        canvas.bind("<ButtonPress-1>", on_down)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_up)
        sel.bind("<Escape>", lambda _: sel.destroy())

    def _resolve_capture_bbox(self) -> tuple[int, int, int, int] | None:
        if self.scr_mode_var.get() == "window":
            if not self.target_hwnd:
                return None
            wrect = get_window_rect(self.target_hwnd)
            if not wrect:
                return None
            if self.rel_bbox:
                dx1, dy1, dx2, dy2 = self.rel_bbox
                return (wrect[0] + dx1, wrect[1] + dy1, wrect[0] + dx2, wrect[1] + dy2)
            return wrect
        return self.abs_bbox

    # -----------------------------------------------------------------
    # 화면 번역 루프
    # -----------------------------------------------------------------
    def toggle_screen(self):
        if not self.screen_on and not self._resolve_capture_bbox():
            self.lbl_scr_status.config(text="⚠️ 먼저 [영역 드래그 지정]을 하거나 대상 창을 선택해 주세요.")
            return
        self.screen_on = not self.screen_on
        if self.screen_on:
            self.btn_scr_toggle.config(text="화면 번역: ON", bg="#a6e3a1", fg="#11111b")
            self._screen_loop()
        else:
            self.btn_scr_toggle.config(text="화면 번역: OFF", bg="#45475a", fg="#cdd6f4")

    def _screen_loop(self):
        if not self.screen_on:
            return
        if not self.screen_busy:
            self.trigger_screen_once(force=False)
        ms = max(500, int(float(self.scr_interval_var.get()) * 1000))
        self.root.after(ms, self._screen_loop)

    def trigger_screen_once(self, force: bool = False):
        bbox = self._resolve_capture_bbox()
        if not bbox or self.screen_busy:
            return
        key = self.api_var.get().strip()
        if not key:
            self.lbl_scr_status.config(text="⚠️ API Key를 먼저 입력해 주세요.")
            return
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        except Exception as e:
            self.lbl_scr_status.config(text=f"캡처 오류: {e}")
            return

        b64, small, changed = encode_And_diff(img, None if force else self.prev_small)
        if not changed:
            return
        self.prev_small = small
        self.screen_busy = True
        self.lbl_scr_status.config(text=f"{self.model_var.get()} 화면 번역 스트리밍 중...")
        threading.Thread(target=self._translate_image_worker, args=(key, b64), daemon=True).start()

    def _translate_image_worker(self, key: str, b64: str):
        t0 = time.perf_counter()
        target_lang = LANGUAGES.get(self.lang_var.get(), "한국어")
        prompt = f"이미지에 보이는 텍스트를 {target_lang}로만 자연스럽고 정확하게 번역해. 부연 설명 없이 번역문만 출력해."
        try:
            client = genai.Client(api_key=key)
            stream = client.interactions.create(
                model=self.model_var.get().strip(),
                input=[
                    {"type": "image", "data": b64, "mime_type": "image/jpeg"},
                    {"type": "text", "text": prompt},
                ],
                generation_config={"thinking_level": "minimal"},
                stream=True,
                store=False,
            )
            first = True
            for event in stream:
                if event.event_type == "step.delta" and getattr(event.delta, "type", None) == "text":
                    chunk = event.delta.text
                    self.root.after(0, self.overlay.update_content, "screen", chunk, not first)
                    first = False
                elif event.event_type == "interaction.completed" and getattr(event.interaction, "usage", None):
                    u = event.interaction.usage
                    self.quota.record(
                        int(getattr(u, "total_input_tokens", 0) or 0),
                        int(getattr(u, "total_output_tokens", 0) or 0),
                        int(getattr(u, "total_tokens", 0) or 0),
                    )
            dt = time.perf_counter() - t0
            self.root.after(0, lambda: self.lbl_scr_status.config(text=f"화면 번역 완료 ({dt:.2f}초)"))
            self.root.after(0, self._refresh_quota_ui)
        except Exception as e:
            self.root.after(0, lambda: self.lbl_scr_status.config(text=f"API 오류: {e}"))
        finally:
            self.screen_busy = False

    # -----------------------------------------------------------------
    # 오디오 프로그램 감지 & 크롬 탭별 오디오 번역
    # -----------------------------------------------------------------
    def refresh_audio_sessions(self):
        sessions = list_audio_sessions()
        items = ["전체 시스템 오디오 (모든 프로그램)"]
        for s in sessions:
            badge = f"🔊 재생 중 ({s['peak']:.2f})" if s["peak"] > 0.005 else "🔈 대기"
            items.append(f"{s['name']} | {badge} (PID:{s['pid']})")
        self.aud_combo["values"] = items
        if items and not self.aud_combo_var.get():
            self.aud_combo.current(0)

    def _selected_audio_process_name(self) -> str | None:
        val = self.aud_combo_var.get()
        if not val or val.startswith("전체 시스템"):
            return None
        return val.split("|")[0].strip()

    def open_chrome_tab_selector(self):
        self.aud_mode_var.set("chrome_tab")
        self.chrome_bridge.start()
        webbrowser.open(f"http://127.0.0.1:{BRIDGE_PORT}")
        self.lbl_aud_status.config(text=f"브라우저에서 번역할 크롬 탭을 선택하세요 (http://127.0.0.1:{BRIDGE_PORT})")

    def toggle_audio(self):
        key = self.api_var.get().strip()
        if not self.audio_on and not key:
            self.lbl_aud_status.config(text="⚠️ API Key를 먼저 입력해 주세요.")
            return
        self.audio_on = not self.audio_on
        if self.audio_on:
            self.btn_aud_toggle.config(text="오디오 번역: ON", bg="#a6e3a1", fg="#11111b")
            if self.aud_mode_var.get() == "chrome_tab":
                self.chrome_bridge.start()
                self.lbl_aud_status.config(text="크롬 탭 오디오 수신 대기 중 (탭 선택 창에서 탭을 공유하세요)")
            else:
                threading.Thread(target=self._audio_loop_worker, daemon=True).start()
        else:
            self.btn_aud_toggle.config(text="오디오 번역: OFF", bg="#45475a", fg="#cdd6f4")
            self.lbl_aud_status.config(text="오디오 번역 중지됨")

    def _audio_loop_worker(self):
        while self.audio_on and self.aud_mode_var.get() == "process":
            target_proc = self._selected_audio_process_name()
            if not is_process_making_sound(target_proc):
                self.root.after(0, lambda p=target_proc: self.lbl_aud_status.config(text=f"'{p}' 소리 대기 중..."))
                time.sleep(0.6)
                continue
            try:
                dur = float(self.aud_chunk_var.get())
                wav_bytes, rms = record_wasapi_loopback_wav(duration_sec=dur)
                if not wav_bytes or rms < 0.005:
                    continue
                b64 = base64.b64encode(wav_bytes).decode("ascii")
                self._translate_audio_b64(target_proc or "시스템 오디오", b64)
            except Exception as e:
                self.root.after(0, lambda err=e: self.lbl_aud_status.config(text=f"오디오 캡처 오류: {err}"))
                time.sleep(1.0)

    def _on_chrome_tab_wav(self, tab_label: str, wav_b64: str):
        if not self.audio_on or self.aud_mode_var.get() != "chrome_tab" or self.audio_busy:
            return
        threading.Thread(target=self._translate_audio_b64, args=(f"Chrome 탭 ({tab_label})", wav_b64), daemon=True).start()

    def _translate_audio_b64(self, source_label: str, wav_b64: str):
        key = self.api_var.get().strip()
        if not key or self.audio_busy:
            return
        self.audio_busy = True
        t0 = time.perf_counter()
        target_lang = LANGUAGES.get(self.lang_var.get(), "한국어")
        prompt = (
            f"오디오에서 들리는 음성을 {target_lang}로만 즉시 번역해. "
            "의미 있는 음성이 없으면 아무것도 출력하지 말고, 음성이 있으면 부연 설명 없이 번역문만 출력해."
        )
        try:
            self.root.after(0, lambda: self.lbl_aud_status.config(text=f"[{source_label}] 오디오 번역 중..."))
            client = genai.Client(api_key=key)
            stream = client.interactions.create(
                model=self.model_var.get().strip(),
                input=[
                    {"type": "audio", "data": wav_b64, "mime_type": "audio/wav"},
                    {"type": "text", "text": prompt},
                ],
                generation_config={"thinking_level": "minimal"},
                stream=True,
                store=False,
            )
            first = True
            for event in stream:
                if event.event_type == "step.delta" and getattr(event.delta, "type", None) == "text":
                    chunk = event.delta.text
                    if chunk.strip() or not first:
                        self.root.after(0, self.overlay.update_content, "audio", chunk, not first)
                        first = False
                elif event.event_type == "interaction.completed" and getattr(event.interaction, "usage", None):
                    u = event.interaction.usage
                    self.quota.record(
                        int(getattr(u, "total_input_tokens", 0) or 0),
                        int(getattr(u, "total_output_tokens", 0) or 0),
                        int(getattr(u, "total_tokens", 0) or 0),
                    )
            dt = time.perf_counter() - t0
            self.root.after(0, lambda: self.lbl_aud_status.config(text=f"[{source_label}] 번역 완료 ({dt:.2f}초)"))
            self.root.after(0, self._refresh_quota_ui)
        except Exception as e:
            self.root.after(0, lambda err=e: self.lbl_aud_status.config(text=f"오디오 API 오류: {err}"))
        finally:
            self.audio_busy = False

    # -----------------------------------------------------------------
    # 토큰 및 할당량 대시보드 갱신
    # -----------------------------------------------------------------
    def _on_tier_changed(self, _event=None):
        preset = TIER_PRESETS.get(self.tier_var.get())
        if preset:
            self.budget_var.set(preset["daily_tokens"])
            self._refresh_quota_ui()

    def _reset_quota(self):
        self.quota.reset()
        self._refresh_quota_ui()

    def _refresh_quota_ui(self):
        preset = TIER_PRESETS.get(self.tier_var.get(), TIER_PRESETS["무료 티어 (Free Tier)"])
        try:
            daily_budget = max(1, int(self.budget_var.get()))
        except Exception:
            daily_budget = preset["daily_tokens"]

        snap = self.quota.snapshot(
            rpm_limit=preset["rpm"],
            tpm_limit=preset["tpm"],
            rpd_limit=preset["rpd"],
            daily_token_budget=daily_budget,
        )
        self.progress["value"] = snap["pct_remaining"]
        self.lbl_quota_main.config(
            text=f"일일 잔여 토큰: {snap['rem_daily_tokens']:,} / {daily_budget:,} ({snap['pct_remaining']:.1f}% 남음)"
        )
        self.lbl_quota_sub.config(
            text=(
                f"• 분당 요청 잔여(RPM): {snap['rem_rpm']:,} / {preset['rpm']:,} 회   "
                f"• 분당 토큰 잔여(TPM): {snap['rem_tpm']:,} / {preset['tpm']:,} tok\n"
                f"• 일일 요청 잔여(RPD): {snap['rem_rpd']:,} / {preset['rpd']:,} 회   "
                f"• 오늘 누적: 입력 {snap['daily_input']:,} + 출력 {snap['daily_output']:,} = 총 {snap['daily_total']:,} tok "
                f"(직전 요청: {snap['last_tokens']:,} tok)"
            )
        )

    def run(self):
        self.root.mainloop()


# =====================================================================
# 8. 자동화 자체 검증 (Self-Test)
# =====================================================================
def self_test():
    # 1. 픽셀 변경 감지 테스트
    img1 = Image.new("RGB", (100, 100), color=(0, 0, 0))
    img2 = Image.new("RGB", (100, 100), color=(255, 255, 255))
    b64, small1, ch1 = encode_And_diff(img1, None)
    assert ch1 is True and len(b64) > 20
    _, _, ch_same = encode_And_diff(img1, small1)
    assert ch_same is False, "동일 화면은 API 호출을 건너뛰어야 함"
    _, _, ch_diff = encode_And_diff(img2, small1)
    assert ch_diff is True, "변경된 화면은 감지되어야 함"

    # 2. Windows 실행 중인 창 목록 탐색 테스트
    wins = list_visible_windows()
    assert isinstance(wins, list), "창 목록 반환 형식 오류"

    # 3. 오디오 사용 프로세스 탐색 테스트
    sessions = list_audio_sessions()
    assert isinstance(sessions, list), "오디오 세션 목록 반환 형식 오류"

    # 4. 토큰 및 할당량 트래커 계산 테스트
    tmp_usage = USAGE_PATH.with_name("test_token_usage.tmp.json")
    try:
        tracker = TokenQuotaTracker(storage_path=tmp_usage)
        tracker.reset()
        tracker.record(in_tok=300, out_tok=50, total_tok=350)
        snap = tracker.snapshot(rpm_limit=15, tpm_limit=250_000, rpd_limit=1_500, daily_token_budget=1_000_000)
        assert snap["rem_rpm"] == 14
        assert snap["rem_tpm"] == 249_650
        assert snap["rem_rpd"] == 1_499
        assert snap["rem_daily_tokens"] == 999_650
    finally:
        if tmp_usage.exists():
            tmp_usage.unlink()

    print("self_test OK")


if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
    else:
        ScreenTranslator().run()
