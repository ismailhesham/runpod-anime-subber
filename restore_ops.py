# -*- coding: utf-8 -*-
"""AI restore stage: optional x4 upscale/cleanup for old low-quality anime.

Models (downloaded on demand into /workspace/models, survives pod restarts):
  - "cugan": Real-CUGAN up4x-latest-denoise3x (bilibili/ailab) — best quality.
  - "v3":    realesr-animevideov3 (xinntao/Real-ESRGAN) — the fast option.

The restored MP4 (h264 intermediate + original audio) replaces the raw video
as the input of the existing translate/burn/compress pipeline.
"""
import os
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np

from media_ops import probe_video_info, _has_nvenc

MODELS_DIR = Path(os.environ.get("RUNPOD_MODELS_ROOT", "/workspace/models"))

_V3_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth"
_V3_FILE = "realesr-animevideov3.pth"
_CUGAN_ZIP_URL = "https://github.com/bilibili/ailab/releases/download/Real-CUGAN/updated_weights.zip"
_CUGAN_ZIP = "cugan_updated_weights.zip"
_CUGAN_MEMBER = "updated_weights/up4x-latest-denoise3x.pth"
_CUGAN_FILE = "up4x-latest-denoise3x.pth"

MODEL_LABELS = {
    "cugan": "Real-CUGAN denoise3x ×4",
    "v3": "realesr-animevideov3 ×4",
}


def _download_with_progress(url: str, dest: Path, log_cb: Callable[[str], None]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log_cb(f"⬇️ تحميل موديل الترميم من: {url}")
    t0 = time.time()
    next_mark = 0

    def _hook(done_blocks: int, block_size: int, total: int) -> None:
        nonlocal next_mark
        if total > 0:
            pct = min(100, int(done_blocks * block_size * 100 / total))
            if pct >= next_mark:
                next_mark = pct + 20
                log_cb(f"⬇️ تحميل الموديل: {pct}% ({total / 1e6:.0f}MB)")

    try:
        urllib.request.urlretrieve(url, str(tmp), reporthook=_hook)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"❌ فشل تحميل موديل الترميم ({url}): {exc}")
    tmp.replace(dest)
    log_cb(f"✓ اكتمل تحميل الموديل ({dest.stat().st_size / 1e6:.1f}MB) في {time.time() - t0:.0f} ثانية → {dest}")


def ensure_restore_model(model_key: str, log_cb: Callable[[str], None]) -> Path:
    """Download (once) and return the weights path for the chosen model."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if model_key == "v3":
        pth = MODELS_DIR / _V3_FILE
        if not pth.is_file():
            _download_with_progress(_V3_URL, pth, log_cb)
        else:
            log_cb(f"✓ موديل {MODEL_LABELS['v3']} موجود مسبقاً: {pth}")
        return pth
    if model_key == "cugan":
        pth = MODELS_DIR / _CUGAN_FILE
        if pth.is_file():
            log_cb(f"✓ موديل {MODEL_LABELS['cugan']} موجود مسبقاً: {pth}")
            return pth
        zpath = MODELS_DIR / _CUGAN_ZIP
        if not zpath.is_file():
            _download_with_progress(_CUGAN_ZIP_URL, zpath, log_cb)
        log_cb("📦 فك ضغط حزمة موديلات Real-CUGAN...")
        with zipfile.ZipFile(str(zpath)) as zf:
            with zf.open(_CUGAN_MEMBER) as src, open(str(pth), "wb") as dst:
                dst.write(src.read())
        log_cb(f"✓ موديل {MODEL_LABELS['cugan']} جاهز: {pth}")
        return pth
    raise RuntimeError(f"❌ موديل ترميم غير معروف: {model_key}")


def _load_model(model_key: str, weights: Path, device, dtype, log_cb):
    import torch
    import restore_nets

    if model_key == "v3":
        sd = torch.load(str(weights), map_location="cpu", weights_only=True)
        params = sd["params"] if "params" in sd else sd
        model = restore_nets.SRVGGNetCompact(
            num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16, upscale=4, act_type="prelu"
        )
        model.load_state_dict(params, strict=True)
    else:
        model = restore_nets.UpCunet4x(in_channels=3, out_channels=3)
        model.load_state_dict(torch.load(str(weights), map_location="cpu"), strict=True)
    model = model.to(device).to(dtype).eval()
    log_cb(f"✓ موديل {MODEL_LABELS[model_key]} اتحمل على الجهاز ({device.type}, {str(dtype).split('.')[-1]}).")
    return model


def _pick_cugan_tuning(px: int, vram_gb: float) -> Tuple[int, int]:
    """(tile_mode, batch_size) لـ CUGAN حسب حجم الفريم وذاكرة الكرت.
    tile_mode=0 يعني الفريم كاملاً دفعة واحدة (أسرع بكتير) لو الذاكرة تكفي."""
    if px <= 640 * 480:
        return 0, 4
    if px <= 1280 * 720:
        if vram_gb >= 14:
            return 0, 2
        if vram_gb >= 8:
            return 1, 2
        return 2, 1
    if vram_gb >= 16:
        return 0, 1
    if vram_gb >= 10:
        return 1, 1
    if vram_gb >= 6:
        return 2, 1
    return 3, 1


def _pick_v3_batch(px: int, vram_gb: float) -> int:
    """حجم الدفعة لموديل v3 الخفيف حسب الذاكرة."""
    if px <= 640 * 480:
        return 4
    if px <= 1280 * 720:
        return 4 if vram_gb >= 10 else 2
    return 4 if vram_gb >= 12 else (2 if vram_gb >= 8 else 1)


def _sr_batch(model, model_key: str, frames: np.ndarray, device, dtype, tile_mode: int) -> np.ndarray:
    """Run a batch of RGB uint8 frames (n,h,w,3) -> x4 RGB uint8 (n,4h,4w,3)."""
    import torch

    x = torch.from_numpy(frames).to(device).permute(0, 3, 1, 2).contiguous()
    x = x.to(dtype).div_(255.0)
    with torch.no_grad():
        if model_key == "v3":
            out = model(x)  # float 0..1
            out = out.mul_(255.0).round_().clamp_(0, 255).byte()
        else:
            out = model(x, tile_mode=tile_mode, cache_mode=0, alpha=1.0, pro=False)  # uint8
    return out.permute(0, 2, 3, 1).contiguous().cpu().numpy()


def restore_video(
    video_path: str,
    out_path: str,
    model_key: str = "cugan",
    log_cb: Callable[[str], None] = print,
    progress_cb: Optional[Callable[[float, str, str], None]] = None,
    max_seconds: Optional[float] = None,
    start_seconds: Optional[float] = None,
    crf: int = 14,
) -> str:
    """Restore a video x4 with the chosen model. Fails loudly without a CUDA GPU."""
    t_start = time.time()

    def log(msg: str) -> None:
        log_cb(msg)

    def prog(pct: float, metric: str, detail: str) -> None:
        if progress_cb is not None:
            progress_cb(pct, metric, detail)

    try:
        import torch
    except ImportError:
        raise RuntimeError(
            "❌ الترميم يحتاج مكتبة torch وهي غير مثبتة على هذا البود — نفّذ: pip install torch"
        )
    dev_override = os.environ.get("RUNPOD_RESTORE_DEVICE", "").strip().lower()
    if dev_override == "cpu":
        device = torch.device("cpu")
        dtype = torch.float32
        log("⚠️ وضع اختبار: الترميم على المعالج (RUNPOD_RESTORE_DEVICE=cpu) — بطيء جداً.")
    else:
        if not torch.cuda.is_available():
            raise RuntimeError(
                "❌ الترميم يحتاج كرت شاشة CUDA ولم يتم العثور عليه (torch.cuda.is_available() = False) "
                "— شغّل البود بكرت GPU أو عطّل خطوة الترميم."
            )
        import torch.backends.cudnn as cudnn

        cudnn.benchmark = True
        device = torch.device("cuda")
        dtype = torch.float16
        log(f"🎮 كرت الشاشة: {torch.cuda.get_device_name(0)}")

    label = MODEL_LABELS.get(model_key, model_key)
    w, h, fps, dur = probe_video_info(video_path)
    if dur <= 0:
        dur = 0.0
    eff_seconds = min(dur, max_seconds) if (max_seconds and dur > 0) else max_seconds
    start_at = 0.0
    if start_seconds and dur > 0:
        start_at = max(0.0, min(float(start_seconds), max(0.0, dur - (eff_seconds or 0.0))))
    est_frames = int((eff_seconds or dur or 0) * fps)
    log(
        f"🧪 بدء الترميم بـ {label}: {w}x{h} → {w * 4}x{h * 4} · {fps:.3f}fps"
        + ((f" · معاينة {eff_seconds:g} ثانية" + (f" من منتصف الحلقة (تبدأ ~{int(start_at // 60)}:{int(start_at % 60):02d})" if start_at > 0 else " من البداية")) if eff_seconds else f" · {est_frames} فريم تقريباً")
    )

    weights = ensure_restore_model(model_key, log)
    model = _load_model(model_key, weights, device, dtype, log)

    px = w * h
    vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9 if device.type == "cuda" else 0.0
    if model_key == "cugan":
        tile_mode, batch_size = _pick_cugan_tuning(px, vram_gb)
    else:
        tile_mode = 0
        batch_size = _pick_v3_batch(px, vram_gb)
    log(
        f"⚙️ إعدادات المعالجة: batch={batch_size}"
        + (f" · tile_mode={tile_mode}" if model_key == "cugan" else "")
        + (f" · VRAM={vram_gb:.0f}GB" if vram_gb else "")
    )

    ow, oh = w * 4, h * 4
    dec_cmd = ["ffmpeg", "-v", "error"]
    if start_at > 0:
        dec_cmd += ["-ss", f"{start_at:.3f}"]
    if eff_seconds:
        dec_cmd += ["-t", f"{eff_seconds:.3f}"]
    dec_cmd += ["-i", video_path, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]

    use_nv = _has_nvenc("hevc_nvenc") and device.type == "cuda"
    if use_nv:
        log("⚡ ترميز الناتج على كرت الشاشة (hevc_nvenc) — أسرع بكتير من المعالج خصوصاً مع دقة ×4.")
    else:
        log("⚠️ ترميز الناتج على المعالج (libx264) — hevc_nvenc غير متاح.")

    def _build_enc_cmd(nv: bool) -> List[str]:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}", "-r", f"{fps:.6f}", "-i", "pipe:0",
        ]
        if start_at > 0:
            cmd += ["-ss", f"{start_at:.3f}"]
        if eff_seconds:
            cmd += ["-t", f"{eff_seconds:.3f}"]
        cmd += ["-i", video_path, "-map", "0:v", "-map", "1:a?"]
        if nv:
            cmd += [
                "-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(min(24, max(14, crf + 2))), "-b:v", "0",
                "-spatial-aq", "1", "-temporal-aq", "1", "-pix_fmt", "yuv420p",
                "-bf", "3", "-tag:v", "hvc1",
            ]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest", out_path]
        return cmd

    frame_bytes = w * h * 3

    while True:
        done_frames = 0
        batch: list = []
        fps_win_t = time.time()
        fps_win_n = 0
        enc_died_early = False

        dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        enc = subprocess.Popen(_build_enc_cmd(use_nv), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def _flush_batch() -> None:
            nonlocal done_frames, fps_win_t, fps_win_n
            if not batch:
                return
            arr = np.stack(batch, axis=0)
            out = _sr_batch(model, model_key, arr, device, dtype, tile_mode)
            enc.stdin.write(out.tobytes())
            done_frames += len(batch)
            fps_win_n += len(batch)
            batch.clear()
            now = time.time()
            if now - fps_win_t >= 2.0:
                cur_fps = fps_win_n / max(0.001, now - fps_win_t)
                fps_win_t, fps_win_n = now, 0
                pct = (done_frames / est_frames * 100.0) if est_frames else 0.0
                remaining = est_frames - done_frames
                eta_s = (remaining / cur_fps) if (remaining > 0 and cur_fps > 0) else 0
                eta_txt = f" · متبقٍ ~{int(eta_s // 60)}:{int(eta_s % 60):02d}" if (eta_s > 0 and pct < 99.0) else ""
                prog(
                    min(99.0, pct),
                    f"ترميم: {done_frames}/{est_frames or '؟'} فريم",
                    f"{cur_fps:.1f} فريم/ث" + eta_txt,
                )

        try:
            while True:
                buf = dec.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
                batch.append(frame)
                if len(batch) >= batch_size:
                    _flush_batch()
                    if enc.poll() is not None:
                        enc_died_early = True
                        break
            if not enc_died_early:
                _flush_batch()
        except torch.cuda.OutOfMemoryError:
            for p in (dec, enc):
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            if done_frames == 0 and model_key == "cugan" and tile_mode < 4:
                tile_mode += 1
                batch_size = 1
                log(f"⚠️ ذاكرة الكرت امتلأت — تقليل حجم القطعة تلقائياً (tile_mode={tile_mode}) وإعادة المحاولة.")
                continue
            if done_frames == 0 and model_key != "cugan" and batch_size > 1:
                batch_size = max(1, batch_size // 2)
                log(f"⚠️ ذاكرة الكرت امتلأت — تقليل الدفعة إلى {batch_size} وإعادة المحاولة.")
                continue
            try:
                Path(out_path).unlink(missing_ok=True)
            except Exception:
                pass
            raise

        if enc_died_early:
            try:
                dec.kill()
            except Exception:
                pass
        try:
            enc.stdin.close()
        except Exception:
            pass
        dec_rc = dec.wait()
        enc_rc = enc.wait()
        if (enc_died_early or enc_rc != 0) and done_frames == 0 and use_nv:
            log("⚠️ ترميز الكرت فشل من البداية — الرجوع إلى ترميز المعالج (libx264) وإعادة المحاولة.")
            use_nv = False
            try:
                Path(out_path).unlink(missing_ok=True)
            except Exception:
                pass
            continue
        if enc_died_early or enc_rc != 0:
            err = enc.stderr.read().decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"❌ فشل ترميز الفيديو المرمَّم: {err}")
        if dec_rc != 0:
            err = dec.stderr.read().decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"❌ فشل فك ترميز الفيديو أثناء الترميم: {err}")
        break

    elapsed = time.time() - t_start
    out_mb = Path(out_path).stat().st_size / 1e6 if Path(out_path).is_file() else 0.0
    avg_fps = done_frames / max(0.001, elapsed)
    log(
        f"✅ اكتمل الترميم: {done_frames} فريم في {int(elapsed // 60)}:{int(elapsed % 60):02d}"
        f" ({avg_fps:.1f} فريم/ث) → {Path(out_path).name} ({out_mb:.0f}MB)"
    )
    prog(100.0, "اكتمل الترميم", f"{avg_fps:.1f} فريم/ث")
    del model
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass
    return out_path
