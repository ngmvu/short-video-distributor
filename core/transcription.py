from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from core.config import ROOT_DIR, STORAGE_DIR, get_setting
from core.storage import write_json_atomic

LOGGER = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
DOWNLOAD_DIR = STORAGE_DIR / "downloads" / "transcription_sources"
TRANSCRIPT_DIR = STORAGE_DIR / "transcripts"

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
}
CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
}


def load_authorized_urls(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"URL list file does not exist: {path}")

    urls: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"Line {line_number}: expected an HTTPS direct media URL.")
        if parsed.username or parsed.password:
            raise ValueError(f"Line {line_number}: URLs must not contain credentials.")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError(f"Line {line_number}: non-public IP addresses are not allowed.")
        urls.append(value)

    if not urls:
        raise ValueError(f"No HTTPS media URLs found in {path}.")
    return urls


def _safe_source_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _media_extension(content_type: str, url: str) -> str:
    content_type = content_type.partition(";")[0].strip().lower()
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if extension:
        return extension
    if content_type == "application/octet-stream":
        url_extension = Path(urlsplit(url).path).suffix.lower()
        if url_extension in MEDIA_EXTENSIONS:
            return url_extension
    raise ValueError(
        "URL did not return a supported media file. Use a direct video/audio URL, "
        "not a TikTok page link."
    )


def download_media(url: str, download_dir: Path = DOWNLOAD_DIR) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    temporary_path: Path | None = None
    download_dir.mkdir(parents=True, exist_ok=True)

    with requests.get(
        url,
        stream=True,
        timeout=(15, 90),
        allow_redirects=False,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        extension = _media_extension(content_type, url)
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise ValueError("Media file exceeds the 500 MB download limit.")

        destination = download_dir / f"{digest}{extension}"
        temporary_path = download_dir / f".{digest}{extension}.part"
        bytes_written = 0
        try:
            with temporary_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    bytes_written += len(chunk)
                    if bytes_written > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Media file exceeds the 500 MB download limit.")
                    output.write(chunk)
            if bytes_written == 0:
                raise ValueError("Media download was empty.")
            os.replace(temporary_path, destination)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    return destination


def _ensure_windows_vc_runtime() -> None:
    """Preload a modern msvcp140.dll (v14.40+) bundled in site-packages (e.g. numpy.libs) so
    CTranslate2 and ONNX Runtime do not crash with 0xC0000005 on systems with an older
    C:\\Windows\\System32\\msvcp140.dll (pre-VS 2022 17.10 std::mutex ABI change)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        import shutil
        import site

        for site_dir in site.getsitepackages():
            sp = Path(site_dir)
            candidates = sorted((sp / "numpy.libs").glob("msvcp140*.dll"))
            if not candidates:
                continue
            src = candidates[0]
            loaded = False
            for rel_target in (Path("ctranslate2") / "msvcp140.dll", Path("onnxruntime") / "capi" / "msvcp140.dll"):
                target = sp / rel_target
                if target.parent.is_dir():
                    if not target.is_file():
                        try:
                            shutil.copy2(src, target)
                        except OSError:
                            pass
                    if target.is_file() and not loaded:
                        ctypes.WinDLL(str(target))
                        loaded = True
            if not loaded:
                ctypes.WinDLL(str(src))
            return
    except Exception:
        pass


def _available_memory_mb() -> float | None:
    """Return the minimum of free physical RAM and remaining Windows commit limit in MB."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            avail_phys = status.ullAvailPhys / (1024 * 1024)
            avail_commit = status.ullAvailPageFile / (1024 * 1024)
            return min(avail_phys, avail_commit)
    except Exception:
        pass
    return None


def _is_oom_error(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    msg = str(exc).lower()
    return "mkl_malloc" in msg or "failed to allocate memory" in msg or "out of memory" in msg


def load_whisper_model(model_size: str) -> Any:
    _ensure_windows_vc_runtime()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc
    # Weak CPUs: WHISPER_COMPUTE_TYPE=int8 (default, fastest) | float32 (if int8 crashes);
    # WHISPER_CPU_THREADS=0 lets CTranslate2 pick, or set it to the number of physical cores.
    compute_type = (os.getenv("WHISPER_COMPUTE_TYPE") or "int8").strip()
    threads_raw = (os.getenv("WHISPER_CPU_THREADS") or "0").strip()
    cpu_threads = int(threads_raw) if threads_raw.isdigit() else 0

    avail_mb = _available_memory_mb()
    if avail_mb is not None and cpu_threads == 0 and avail_mb < 1800:
        cpu_threads = 2

    fallback_chain = [model_size]
    for smaller in ("base", "tiny"):
        if smaller not in fallback_chain and model_size not in ("tiny",):
            if model_size != "base" or smaller == "tiny":
                fallback_chain.append(smaller)

    if avail_mb is not None:
        if avail_mb < 650 and model_size != "tiny":
            LOGGER.warning(
                "Bộ nhớ trống thấp (%.0f MB); tự động hạ model Whisper từ '%s' xuống 'tiny' để tránh lỗi mkl_malloc.",
                avail_mb,
                model_size,
            )
            fallback_chain = ["tiny"]
        elif avail_mb < 1300 and model_size not in ("base", "tiny"):
            LOGGER.warning(
                "Bộ nhớ trống thấp (%.0f MB); tự động hạ model Whisper từ '%s' xuống 'base' để tránh lỗi mkl_malloc.",
                avail_mb,
                model_size,
            )
            fallback_chain = ["base", "tiny"]

    last_exc: Exception | None = None
    for candidate in fallback_chain:
        try:
            return WhisperModel(
                candidate,
                device="cpu",
                compute_type=compute_type,
                cpu_threads=cpu_threads,
            )
        except Exception as exc:
            last_exc = exc
            if not _is_oom_error(exc) or candidate == fallback_chain[-1]:
                raise
            import gc

            gc.collect()
            LOGGER.warning(
                "Thiếu bộ nhớ khi nạp model Whisper '%s' (%s); đang thử model nhỏ hơn…",
                candidate,
                exc,
            )
    raise RuntimeError(f"Could not load Whisper model: {last_exc}")


_ONNX_EXIT_HINTS = {
    3221225477: "0xC0000005 access violation — thường do Visual C++ Redistributable cũ",
    3221225781: "0xC0000135 thiếu DLL — cần cài Visual C++ Redistributable x64",
    3221225501: "0xC000001D CPU không hỗ trợ tập lệnh",
}
_vad_ok: bool | None = None


def probe_onnxruntime(timeout: float = 120.0) -> tuple[bool, str]:
    """Import onnxruntime in a child process. On some Windows machines the import crashes the
    interpreter natively (no Python exception), so it must never be tried in the bot process first."""
    import subprocess
    import sys

    _ensure_windows_vc_runtime()
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import onnxruntime"],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"không chạy được phép thử: {exc}"
    if result.returncode == 0:
        return True, ""
    code = result.returncode & 0xFFFFFFFF
    stderr = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
    hint = _ONNX_EXIT_HINTS.get(code) or (stderr[-1] if stderr else f"mã thoát {code}")
    return False, hint


def vad_enabled() -> bool:
    """VAD (silence filter) needs onnxruntime. WHISPER_VAD=false turns it off; otherwise it is
    used only when onnxruntime imports cleanly (checked once per process)."""
    global _vad_ok
    if (os.getenv("WHISPER_VAD") or "true").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if _vad_ok is None:
        _vad_ok, reason = probe_onnxruntime()
        if not _vad_ok:
            LOGGER.warning(
                "Tắt bộ lọc VAD của Whisper vì onnxruntime không nạp được (%s). Bóc thoại vẫn chạy, "
                "chỉ chậm hơn ở đoạn im lặng. Cài https://aka.ms/vs/17/release/vc_redist.x64.exe để bật lại.",
                reason,
            )
    return _vad_ok


def transcribe_media(
    model: Any,
    media_path: Path,
    source_url: str,
    model_size: str,
    language: str | None,
    output_dir: Path = TRANSCRIPT_DIR,
    extra: dict[str, Any] | None = None,
) -> Path:
    try:
        segments, info = model.transcribe(
            str(media_path),
            language=language or None,
            vad_filter=vad_enabled(),
        )
        raw_segments = list(segments)
    except Exception as exc:
        if not _is_oom_error(exc):
            raise
        import gc

        gc.collect()
        LOGGER.warning(
            "Thiếu bộ nhớ khi bóc thoại với model '%s' (%s); đang thử lại ở chế độ tiết kiệm RAM (beam_size=1, tắt VAD)…",
            model_size,
            exc,
        )
        raw_segments = None
        for fallback_size in [model_size, "base", "tiny"]:
            try:
                active_model = model if fallback_size == model_size else load_whisper_model(fallback_size)
                segments, info = active_model.transcribe(
                    str(media_path),
                    language=language or None,
                    vad_filter=False,
                    beam_size=1,
                )
                raw_segments = list(segments)
                model_size = fallback_size
                break
            except Exception as retry_exc:
                if not _is_oom_error(retry_exc) or fallback_size == "tiny":
                    raise
                model = None
                gc.collect()
        if raw_segments is None:
            raise

    transcript_segments = [
        {
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
        }
        for segment in raw_segments
    ]
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
    output_path = output_dir / f"{digest}.json"
    write_json_atomic(
        output_path,
        {
            "source_url": _safe_source_url(source_url),
            "media_file": media_path.name,
            "model": model_size,
            "language": info.language,
            "duration_seconds": round(info.duration, 3),
            **(extra or {}),
            "segments": transcript_segments,
        },
    )
    return output_path


def process_url_list(
    url_list: Path,
    model_size: str = "small",
    language: str | None = "vi",
    download_dir: Path = DOWNLOAD_DIR,
    output_dir: Path = TRANSCRIPT_DIR,
    model: Any | None = None,
) -> list[Path]:
    urls = load_authorized_urls(url_list)
    transcription_model = model if model is not None else load_whisper_model(model_size)
    transcripts: list[Path] = []
    for index, url in enumerate(urls, 1):
        LOGGER.info("Downloading authorized media %d/%d.", index, len(urls))
        media_path = download_media(url, download_dir)
        transcript_path = transcribe_media(
            transcription_model,
            media_path,
            url,
            model_size,
            language,
            output_dir,
        )
        transcripts.append(transcript_path)
        LOGGER.info("Saved transcript to %s.", transcript_path)
    return transcripts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Download authorized direct media URLs and transcribe them to timestamped JSON."
    )
    parser.add_argument(
        "url_list",
        type=Path,
        help="UTF-8 text file containing one HTTPS direct media URL per line.",
    )
    parser.add_argument("--model", default="small", help="Whisper model size (default: small).")
    parser.add_argument("--language", default="vi", help="Audio language code, or 'auto'.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT_DIR / "storage" / "transcripts",
        help="Directory for timestamped JSON files.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    language = None if args.language.lower() == "auto" else args.language
    process_url_list(
        args.url_list,
        model_size=args.model,
        language=language,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
