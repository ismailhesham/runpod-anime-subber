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

import re

import numpy as np

from media_ops import probe_video_info, _has_nvenc

MODELS_DIR = Path(os.environ.get("RUNPOD_MODELS_ROOT", "/workspace/models"))

_V3_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth"
_V3_FILE = "realesr-animevideov3.pth"
_CUGAN_ZIP_URL = "https://github.com/bilibili/ailab/releases/download/Real-CUGAN/updated_weights.zip"
_CUGAN_ZIP = "cugan_updated_weights.zip"


def _cugan_weight_name(scale: int) -> str:
    return f"up{scale}x-latest-denoise3x.pth"

MODEL_LABELS = {
    "cugan": "Real-CUGAN denoise3x",
    "v3": "Real-ESRGAN animevideo v3",
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


def ensure_restore_model(model_key: str, log_cb: Callable[[str], None], scale: int = 4) -> Path:
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
        scale = scale if scale in (2, 3, 4) else 4
        fname = _cugan_weight_name(scale)
        pth = MODELS_DIR / fname
        if pth.is_file():
            log_cb(f"✓ موديل Real-CUGAN denoise3x ×{scale} موجود مسبقاً: {pth}")
            return pth
        zpath = MODELS_DIR / _CUGAN_ZIP
        if not zpath.is_file():
            _download_with_progress(_CUGAN_ZIP_URL, zpath, log_cb)
        log_cb(f"📦 فك ضغط موديل ×{scale} من حزمة Real-CUGAN...")
        with zipfile.ZipFile(str(zpath)) as zf:
            with zf.open(f"updated_weights/{fname}") as src, open(str(pth), "wb") as dst:
                dst.write(src.read())
        log_cb(f"✓ موديل Real-CUGAN denoise3x ×{scale} جاهز: {pth}")
        return pth
    raise RuntimeError(f"❌ موديل ترميم غير معروف: {model_key}")


_MODEL_CACHE: dict = {}
_IVTC_CACHE: dict = {}


def _load_model(model_key: str, weights: Path, device, dtype, log_cb, scale: int = 4):
    import torch
    import restore_nets

    cache_key = (model_key, scale, str(device), str(dtype))
    if cache_key in _MODEL_CACHE:
        log_cb("✓ الموديل جاهز في الذاكرة من التشغيل السابق — بدون إعادة تحميل (التسخين مرة واحدة لكل تشغيلة).")
        return _MODEL_CACHE[cache_key]
    t0 = time.time()

    if model_key == "v3":
        sd = torch.load(str(weights), map_location="cpu", weights_only=True)
        params = sd["params"] if "params" in sd else sd
        model = restore_nets.SRVGGNetCompact(
            num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16, upscale=4, act_type="prelu"
        )
        model.load_state_dict(params, strict=True)
    else:
        cls = {2: restore_nets.UpCunet2x, 3: restore_nets.UpCunet3x, 4: restore_nets.UpCunet4x}.get(scale, restore_nets.UpCunet4x)
        model = cls(in_channels=3, out_channels=3)
        model.load_state_dict(torch.load(str(weights), map_location="cpu"), strict=True)
    model = model.to(device).to(dtype).eval()
    if device.type == "cuda":
        model = model.to(memory_format=torch.channels_last)
    _MODEL_CACHE[cache_key] = model
    name = MODEL_LABELS[model_key] if model_key == "v3" else f"Real-CUGAN denoise3x ×{scale}"
    log_cb(f"✓ موديل {name} اتحمل على الجهاز ({device.type}, {str(dtype).split('.')[-1]}) في {time.time() - t0:.0f} ثانية.")
    return model


def _pick_cugan_tuning(px: int, vram_gb: float) -> Tuple[int, int]:
    """(tile_mode, batch_size) لـ CUGAN حسب حجم الفريم وذاكرة الكرت.
    tile_mode=0 يعني الفريم كاملاً دفعة واحدة (أسرع بكتير) لو الذاكرة تكفي."""
    if px <= 640 * 480:
        return 0, 12
    if px <= 1280 * 720:
        if vram_gb >= 14:
            return 0, 8
        if vram_gb >= 8:
            return 1, 4
        return 2, 2
    if vram_gb >= 16:
        return 0, 2
    if vram_gb >= 10:
        return 1, 1
    if vram_gb >= 6:
        return 2, 1
    return 3, 1


def _pick_v3_batch(px: int, vram_gb: float) -> int:
    """حجم الدفعة لموديل v3 الخفيف حسب الذاكرة."""
    if px <= 640 * 480:
        return 24
    if px <= 1280 * 720:
        return 16 if vram_gb >= 14 else (8 if vram_gb >= 10 else 4)
    return 4 if vram_gb >= 12 else (2 if vram_gb >= 8 else 1)


def _sr_batch(model, model_key: str, frames: np.ndarray, device, dtype, tile_mode: int, target_hw: Optional[Tuple[int, int]] = None) -> np.ndarray:
    """Run a batch of RGB uint8 frames (n,h,w,3) -> upscaled RGB uint8.
    v3 يكبّر ×4 دائماً؛ لو الهدف أصغر يُصغّر على الكرت مباشرة (أسرع بكتير من lanczos على المعالج)."""
    import torch

    x = torch.from_numpy(frames)
    if device.type == "cuda":
        x = x.pin_memory().to(device, non_blocking=True)
        x = x.permute(0, 3, 1, 2).contiguous(memory_format=torch.channels_last)
    else:
        x = x.to(device).permute(0, 3, 1, 2).contiguous()
    x = x.to(dtype).div_(255.0)
    with torch.no_grad():
        if model_key == "v3":
            out = model(x)  # float 0..1
            if target_hw is not None and (out.shape[2], out.shape[3]) != target_hw:
                out = torch.nn.functional.interpolate(out, size=target_hw, mode="bicubic", align_corners=False, antialias=True)
            out = out.mul_(255.0).round_().clamp_(0, 255).byte()
        else:
            out = model(x, tile_mode=tile_mode, cache_mode=0, alpha=1.0, pro=False)  # uint8
    return out.permute(0, 2, 3, 1).contiguous().cpu().numpy()


def _detect_telecine(video_path: str, sample_ss: float, log: Callable[[str], None]) -> Optional[int]:
    try:
        ck = (video_path, os.path.getmtime(video_path), round(float(sample_ss), 1))
        if ck in _IVTC_CACHE:
            log("✓ نتيجة فحص التليسين محفوظة من المرة السابقة.")
            return _IVTC_CACHE[ck]
    except Exception:
        ck = None
    t0 = time.time()
    res = _detect_telecine_uncached(video_path, sample_ss, log)
    log(f"⏱️ فحص التليسين أخذ {time.time() - t0:.0f} ثانية.")
    if ck:
        _IVTC_CACHE[ck] = res
    return res


def _detect_telecine_uncached(video_path: str, sample_ss: float, log: Callable[[str], None]) -> Optional[int]:
    """يكتشف مصادر DVD المسحوقة تليسين (interlaced 29.97) عبر idet على عينة قصيرة.
    يعيد ترتيب الحقول (1=TFF أو 0=BFF) لو المصدر متشابك، و None لو progressive."""
    try:
        cmd = [
            "ffmpeg", "-hide_banner", "-ss", f"{max(0.0, sample_ss):.3f}", "-t", "8",
            "-i", video_path, "-vf", "idet", "-an", "-f", "null", "-",
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = (p.stderr or "") + (p.stdout or "")
        multi = None
        for line in out.splitlines():
            if "Multi frame detection:" in line:
                multi = line
        if not multi:
            log("🔎 فحص idet: لا توجد قراءة — اعتبار المصدر progressive.")
            return None
        nums = dict(re.findall(r"(TFF|BFF|Progressive|Undetermined):\s*(\d+)", multi))
        tff = int(nums.get("TFF", "0"))
        bff = int(nums.get("BFF", "0"))
        prog_n = int(nums.get("Progressive", "0"))
        log(f"🔎 فحص نوع الإطارات (idet): TFF={tff} · BFF={bff} · Progressive={prog_n}")
        if (tff + bff) > prog_n:
            return 1 if tff >= bff else 0
        return None
    except Exception as exc:
        log(f"⚠️ تعذر فحص التليسين ({exc}) — اعتبار المصدر progressive.")
        return None


def restore_video(
    video_path: str,
    out_path: str,
    model_key: str = "cugan",
    log_cb: Callable[[str], None] = print,
    progress_cb: Optional[Callable[[float, str, str], None]] = None,
    max_seconds: Optional[float] = None,
    start_seconds: Optional[float] = None,
    crf: int = 14,
    scale: int = 4,
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

    if scale not in (2, 3, 4):
        scale = 4
    model_scale = 4 if model_key == "v3" else scale
    label = f"Real-CUGAN denoise3x ×{scale}" if model_key == "cugan" else MODEL_LABELS.get(model_key, model_key)
    w, h, fps, dur = probe_video_info(video_path)
    if dur <= 0:
        dur = 0.0
    eff_seconds = min(dur, max_seconds) if (max_seconds and dur > 0) else max_seconds
    start_at = 0.0
    if start_seconds and dur > 0:
        start_at = max(0.0, min(float(start_seconds), max(0.0, dur - (eff_seconds or 0.0))))

    out_fps = fps
    dec_vf: Optional[str] = None
    ivtc_env = os.environ.get("RUNPOD_RESTORE_IVTC", "auto").strip().lower()
    if ivtc_env != "off" and 25.0 < fps <= 31.0 and (dur or 0) > 20:
        order = 1 if ivtc_env == "force" else _detect_telecine(video_path, start_at, log)
        if order is not None:
            dec_vf = f"fieldmatch=order={order}:combmatch=full,yadif=deint=interlaced,decimate"
            out_fps = fps * 4.0 / 5.0
            log(f"🎞️ المصدر متشابك/مسحوق تليسين (DVD قديم) — IVTC تلقائي: إزالة التشابك واسترجاع {out_fps:.3f}fps قبل الترميم (ده كان سبب الحركة المهزوزة).")
    est_frames = int((eff_seconds or dur or 0) * out_fps)
    log(
        f"🧪 بدء الترميم بـ {label}: {w}x{h} → {w * scale}x{h * scale}"
        + (f" (v3 ×4 ثم تصغير على الكرت)" if model_scale != scale else "")
        + f" · {out_fps:.3f}fps"
        + ((f" · معاينة {eff_seconds:g} ثانية" + (f" من منتصف الحلقة (تبدأ ~{int(start_at // 60)}:{int(start_at % 60):02d})" if start_at > 0 else " من البداية")) if eff_seconds else f" · {est_frames} فريم تقريباً")
    )

    if w >= 1920 or h >= 1080:
        log("💡 تنبيه: المصدر 1080p أو أعلى (غالباً نسخة BD نظيفة) — الترميم بيكلف وقت كبير جداً هنا ومكسبه على الصورة محدود. لو هدفك ترجمة + حرق + ضغط فقط، الأوفر ليك إلغاء الترميم لهذا المصدر.")
    weights = ensure_restore_model(model_key, log, scale)
    model = _load_model(model_key, weights, device, dtype, log, scale)

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

    ow, oh = w * scale, h * scale
    dec_cmd = ["ffmpeg", "-v", "error"]
    if start_at > 0:
        dec_cmd += ["-ss", f"{start_at:.3f}"]
    if eff_seconds:
        dec_cmd += ["-t", f"{eff_seconds:.3f}"]
    dec_cmd += ["-i", video_path]
    if dec_vf:
        dec_cmd += ["-vf", dec_vf]
    dec_cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]

    use_nv = _has_nvenc("hevc_nvenc") and device.type == "cuda"
    if use_nv:
        log("⚡ ترميز الناتج على كرت الشاشة (hevc_nvenc) — أسرع بكتير من المعالج خصوصاً مع دقة ×4.")
    else:
        log("⚠️ ترميز الناتج على المعالج (libx264) — hevc_nvenc غير متاح.")

    def _build_enc_cmd(nv: bool) -> List[str]:
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}", "-r", f"{out_fps:.6f}", "-i", "pipe:0",
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
    t_proc = time.time()

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
            out = _sr_batch(model, model_key, arr, device, dtype, tile_mode, target_hw=(oh, ow))
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
            if done_frames == 0 and model_key == "cugan" and batch_size > 8:
                batch_size = max(8, batch_size // 2)
                log(f"⚠️ ذاكرة الكرت امتلأت — تقليل الدفعة إلى {batch_size} وإعادة المحاولة.")
                continue
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
