"""Diagnose why Whisper (faster-whisper) does not run on this machine.

    python scripts\\check_whisper.py            # uses WHISPER_MODEL from .env (default small)
    python scripts\\check_whisper.py --model tiny

Every step prints before it starts, so if the process dies without a traceback (native crash in
ctranslate2 / onnxruntime), the last "▶" line shows which step killed it.
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import time
import traceback
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def step(title: str) -> None:
    print(f"\n▶ {title}", flush=True)


def ok(text: str) -> None:
    print(f"  ✅ {text}", flush=True)


def fail(text: str, exc: BaseException | None = None) -> None:
    print(f"  ❌ {text}", flush=True)
    if exc is not None:
        print("  " + "".join(traceback.format_exception_only(type(exc), exc)).strip(), flush=True)


def cpu_features() -> dict[str, bool]:
    if os.name != "nt":
        return {}
    import ctypes

    present = ctypes.windll.kernel32.IsProcessorFeaturePresent
    # PF_XMMI64 (SSE2)=10, PF_SSE3=13, PF_SSE4_1=37, PF_SSE4_2=38, PF_AVX=39, PF_AVX2=40, PF_AVX512F=41
    return {name: bool(present(code)) for name, code in
            (("SSE4.1", 37), ("SSE4.2", 38), ("AVX", 39), ("AVX2", 40), ("AVX512F", 41))}


def ram_gb() -> tuple[float, float]:
    if os.name != "nt":
        return 0.0, 0.0
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys / 2**30, status.ullAvailPhys / 2**30


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT_DIR / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.getenv("WHISPER_MODEL", "small") or "small")
    args = parser.parse_args()

    step("Máy & Python")
    print(f"  CPU: {platform.processor() or '?'} · {os.cpu_count()} luồng", flush=True)
    total, free = ram_gb()
    if total:
        print(f"  RAM: {total:.1f} GB, còn trống {free:.1f} GB", flush=True)
    print(f"  Python {sys.version.split()[0]} ({platform.architecture()[0]}) · {sys.executable}", flush=True)
    features = cpu_features()
    if features:
        print("  Tập lệnh: " + ", ".join(f"{k}={'có' if v else 'KHÔNG'}" for k, v in features.items()), flush=True)
        if not features.get("AVX"):
            fail("CPU không có AVX — bản ctranslate2 cài từ pip sẽ crash. Cần máy khác hoặc build riêng.")
    print(f"  WHISPER_MODEL={os.getenv('WHISPER_MODEL', '(trống → small)')} · "
          f"WHISPER_COMPUTE_TYPE={os.getenv('WHISPER_COMPUTE_TYPE', '(trống → int8)')} · "
          f"WHISPER_CPU_THREADS={os.getenv('WHISPER_CPU_THREADS', '(trống → tự động)')}", flush=True)

    step("Import ctranslate2")
    try:
        import ctranslate2

        ok(f"ctranslate2 {ctranslate2.__version__}; compute type CPU hỗ trợ: "
           f"{sorted(ctranslate2.get_supported_compute_types('cpu'))}")
    except Exception as exc:
        fail("Không import được ctranslate2. Nếu lỗi 'DLL load failed' → cài Microsoft Visual C++ "
             "Redistributable x64 (https://aka.ms/vs/17/release/vc_redist.x64.exe) rồi thử lại.", exc)
        return 1

    step("Import faster_whisper + onnxruntime (VAD)")
    try:
        import faster_whisper

        ok(f"faster-whisper {faster_whisper.__version__}")
    except Exception as exc:
        fail("Không import được faster_whisper. Chạy: python -m pip install -r requirements.txt", exc)
        return 1
    try:
        import onnxruntime

        ok(f"onnxruntime {onnxruntime.__version__}")
    except Exception as exc:
        fail("Không import được onnxruntime (dùng cho bộ lọc VAD). Cài Visual C++ Redistributable x64 "
             "hoặc: python -m pip install --force-reinstall onnxruntime", exc)

    step(f"Tải / nạp model '{args.model}' (lần đầu cần Internet tới huggingface.co)")
    from core.transcription import load_whisper_model

    started = time.monotonic()
    try:
        model = load_whisper_model(args.model)
        ok(f"Nạp xong sau {time.monotonic() - started:.1f}s")
    except Exception as exc:
        fail("Không nạp được model. Lỗi mạng/HuggingFace → kiểm tra Internet; MemoryError → dùng "
             "WHISPER_MODEL=base hoặc tiny.", exc)
        return 1

    import numpy as np

    sample_rate = 16_000
    seconds = 10
    t = np.arange(sample_rate * seconds) / sample_rate
    audio = (0.1 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 3 * t) > 0)).astype(np.float32)

    step(f"Chạy thử Whisper trên {seconds}s âm thanh (không VAD)")
    started = time.monotonic()
    try:
        segments, info = model.transcribe(audio, language="vi", vad_filter=False)
        list(segments)
        took = time.monotonic() - started
        ok(f"Xong sau {took:.1f}s (≈ {took / seconds:.1f}s xử lý cho mỗi giây âm thanh)")
        if took / seconds > 2:
            print("  ⚠️ Máy chậm: video 60s sẽ mất khoảng "
                  f"{took / seconds * 60 / 60:.0f} phút. Nên đặt WHISPER_MODEL=base (hoặc tiny).", flush=True)
    except Exception as exc:
        fail("Whisper lỗi khi chạy.", exc)
        return 1

    step("Chạy thử với bộ lọc VAD (giống bot Telegram)")
    try:
        segments, _ = model.transcribe(audio, language="vi", vad_filter=True)
        list(segments)
        ok("VAD chạy được")
    except Exception as exc:
        fail("VAD (onnxruntime) lỗi — Whisper vẫn chạy được nếu tắt VAD.", exc)
        return 1

    print("\n🎉 Whisper chạy được trên máy này.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
