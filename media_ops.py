import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts"}

REQUIRED_FONT_FILES = [
    "AFSANEH_v2 KHWF.otf",
    "Bahij Nassim Bold.ttf",
    "Bahij_Mitra-Regular.ttf",
    "BKR Shahd Extra Bold.ttf",
    "Hayah.otf",
    "Loving Stay.otf",
    "Sovba_Regular.otf",
]

MODELS_CACHE_ROOT = Path(os.environ.get("RUNPOD_MODELS_DIR", "/workspace/models"))
LAMA_TORCHSCRIPT_URL = os.environ.get(
    "LAMA_MODEL_URL",
    "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt",
)
JP_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

ProgressCallback = Callable[[float, str, str], None]

_OCR_READER_LOCK = threading.Lock()
_OCR_READER_INSTANCE = None

_LAMA_LOCK = threading.Lock()
_LAMA_MODEL_INSTANCE = None
_LAMA_DEVICE = None


def verify_required_fonts(fonts_dir: str) -> None:
    """Strictly verify all 7 custom fonts exist in fonts/ so libass never substitutes random fonts."""
    fdir = Path(fonts_dir)
    missing = [f for f in REQUIRED_FONT_FILES if not (fdir / f).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing required font file(s) in {fdir}: {missing}. "
            "Make sure the fonts/ folder was uploaded to GitHub and cloned completely."
        )


def probe_video_info(video_path: str) -> Tuple[int, int, float, float]:
    """Return (width, height, fps, duration_seconds) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json",
        video_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or [{}]
    st = streams[0]
    w = int(st.get("width") or 1920)
    h = int(st.get("height") or 1080)
    fps_raw = str(st.get("r_frame_rate") or "24000/1001")
    try:
        if "/" in fps_raw:
            num, den = fps_raw.split("/", 1)
            fps = float(num) / max(1.0, float(den))
        else:
            fps = float(fps_raw)
    except Exception:
        fps = 23.976
    dur = 0.0
    try:
        dur = float((data.get("format") or {}).get("duration") or 0.0)
    except Exception:
        pass
    return w, h, max(1.0, fps), max(0.0, dur)


def _get_easyocr_reader(log_cb: Optional[Callable[[str], None]] = None):
    global _OCR_READER_INSTANCE
    with _OCR_READER_LOCK:
        if _OCR_READER_INSTANCE is None:
            import easyocr
            import torch

            use_gpu = torch.cuda.is_available()
            ocr_dir = MODELS_CACHE_ROOT / "easyocr"
            ocr_dir.mkdir(parents=True, exist_ok=True)
            if log_cb:
                log_cb(f"👁️ تحميل محرك الرؤية الياباني EasyOCR (GPU={use_gpu}) من {ocr_dir}...")
            _OCR_READER_INSTANCE = easyocr.Reader(
                ["ja", "en"],
                gpu=use_gpu,
                model_storage_directory=str(ocr_dir),
                verbose=False,
            )
        return _OCR_READER_INSTANCE


def _get_lama_model(log_cb: Optional[Callable[[str], None]] = None):
    """Load cached TorchScript big-lama.pt on GPU/CPU with zero Pillow version conflicts."""
    global _LAMA_MODEL_INSTANCE, _LAMA_DEVICE
    with _LAMA_LOCK:
        if _LAMA_MODEL_INSTANCE is not None:
            return _LAMA_MODEL_INSTANCE, _LAMA_DEVICE

        import torch

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        lama_dir = MODELS_CACHE_ROOT / "lama"
        lama_dir.mkdir(parents=True, exist_ok=True)
        model_path = lama_dir / "big-lama.pt"

        if not model_path.is_file() or model_path.stat().st_size < 50 * 1024 * 1024:
            if log_cb:
                log_cb("🎨 تحميل موديل المحو البصري LaMa (big-lama.pt) لأول مرة في /workspace/models/lama...")
            tmp_part = lama_dir / "big-lama.pt.part"
            urllib.request.urlretrieve(LAMA_TORCHSCRIPT_URL, str(tmp_part))
            tmp_part.replace(model_path)

        model = torch.jit.load(str(model_path), map_location=device)
        model.eval()
        model.to(device)
        _LAMA_MODEL_INSTANCE = model
        _LAMA_DEVICE = device
        return _LAMA_MODEL_INSTANCE, _LAMA_DEVICE


def _inpaint_patch_lama_or_telea(
    bgr_patch: np.ndarray,
    mask_u8: np.ndarray,
    log_cb: Optional[Callable[[str], None]] = None,
) -> np.ndarray:
    """Inpaint BGR uint8 patch where mask_u8 > 0 using GPU LaMa with automatic Telea fallback."""
    h, w = bgr_patch.shape[:2]
    try:
        import torch

        model, device = _get_lama_model(log_cb=log_cb)
        rgb = cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        msk = (mask_u8 > 16).astype(np.float32)

        pad_h = (8 - (h % 8)) % 8
        pad_w = (8 - (w % 8)) % 8
        if pad_h or pad_w:
            rgb = np.pad(rgb, ((0, pad_h), (0, pad_w), (0, 0)), mode="symmetric")
            msk = np.pad(msk, ((0, pad_h), (0, pad_w)), mode="symmetric")

        img_t = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(device)
        msk_t = torch.from_numpy(msk).unsqueeze(0).unsqueeze(0).to(device)

        with _LAMA_LOCK, torch.inference_mode():
            out_t = model(img_t, msk_t)

        out_rgb = out_t[0].permute(1, 2, 0).detach().cpu().numpy()
        out_rgb = np.clip(out_rgb[:h, :w] * 255.0, 0, 255).astype(np.uint8)
        return cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)
    except Exception as exc:
        if log_cb:
            log_cb(f"⚠️ التحويل التلقائي لمحرك OpenCV Telea في هذه الرقعة: {exc}")
        return cv2.inpaint(bgr_patch, (mask_u8 > 16).astype(np.uint8) * 255, 5, cv2.INPAINT_TELEA)


def _bgr_to_hex(bgr: Tuple[float, float, float]) -> str:
    b, g, r = [max(0, min(255, int(round(x)))) for x in bgr]
    return f"#{r:02X}{g:02X}{b:02X}"


def _extract_sign_colors_and_mask(bgr_roi: np.ndarray) -> Tuple[str, str, np.ndarray]:
    """Extract dominant text fill color, outline/bg contrast color, and pixel-level stroke mask."""
    rh, rw = bgr_roi.shape[:2]
    if rh < 6 or rw < 6:
        return "#FFFFFF", "#000000", np.ones((rh, rw), dtype=np.uint8) * 255

    border_px = max(2, min(6, rh // 8, rw // 8))
    top_b = bgr_roi[:border_px, :, :].reshape(-1, 3)
    bot_b = bgr_roi[-border_px:, :, :].reshape(-1, 3)
    lft_b = bgr_roi[:, :border_px, :].reshape(-1, 3)
    rgt_b = bgr_roi[:, -border_px:, :].reshape(-1, 3)
    bg_samples = np.vstack([top_b, bot_b, lft_b, rgt_b]).astype(np.float32)
    bg_med = np.median(bg_samples, axis=0)

    diff = np.linalg.norm(bgr_roi.astype(np.float32) - bg_med[None, None, :], axis=2)
    gray = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 55, 150)

    thresh_val = max(26.0, float(np.percentile(diff, 62)))
    fg_mask = ((diff >= thresh_val) | (edges > 0)).astype(np.uint8) * 255

    # Keep interior away from outer 1px frame unless connected to text strokes
    fg_pixels = bgr_roi[fg_mask > 0]
    if len(fg_pixels) >= 12:
        fill_bgr = np.median(fg_pixels, axis=0)
    else:
        fill_bgr = np.array([255.0, 255.0, 255.0])

    # Contrast outline color: dark if fill is bright, otherwise deep dark border
    lum = 0.114 * fill_bgr[0] + 0.587 * fill_bgr[1] + 0.299 * fill_bgr[2]
    if lum > 105:
        outline_bgr = np.percentile(bg_samples, 15, axis=0)
        if (0.114 * outline_bgr[0] + 0.587 * outline_bgr[1] + 0.299 * outline_bgr[2]) > 85:
            outline_bgr = np.array([12.0, 12.0, 12.0])
    else:
        outline_bgr = np.array([248.0, 248.0, 248.0])

    k_size = max(5, int(round(min(rh, rw) * 0.11)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
    dilated = cv2.dilate(fg_mask, kernel, iterations=1)
    return _bgr_to_hex(tuple(fill_bgr)), _bgr_to_hex(tuple(outline_bgr)), dilated


def _merge_frame_detections(raw_dets: List[Tuple[List[float], str, float, float]], frame_w: int, frame_h: int) -> List[Dict]:
    """Merge horizontally/vertically adjacent Japanese text boxes belonging to the same sign."""
    items = []
    for bbox, txt, conf, ang in raw_dets:
        x1, y1, x2, y2 = bbox
        items.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "texts": [(y1, x1, txt)], "conf": conf, "ang": ang})

    merged = True
    while merged and len(items) > 1:
        merged = False
        out = []
        used = [False] * len(items)
        for i in range(len(items)):
            if used[i]:
                continue
            cur = items[i]
            for j in range(i + 1, len(items)):
                if used[j]:
                    continue
                oth = items[j]
                h_ref = max(cur["y2"] - cur["y1"], oth["y2"] - oth["y1"], 1.0)
                x_gap = max(0.0, max(cur["x1"], oth["x1"]) - min(cur["x2"], oth["x2"]))
                y_gap = max(0.0, max(cur["y1"], oth["y1"]) - min(cur["y2"], oth["y2"]))
                if x_gap <= frame_w * 0.045 and y_gap <= h_ref * 0.65:
                    cur = {
                        "x1": min(cur["x1"], oth["x1"]),
                        "y1": min(cur["y1"], oth["y1"]),
                        "x2": max(cur["x2"], oth["x2"]),
                        "y2": max(cur["y2"], oth["y2"]),
                        "texts": cur["texts"] + oth["texts"],
                        "conf": max(cur["conf"], oth["conf"]),
                        "ang": (cur["ang"] + oth["ang"]) * 0.5,
                    }
                    used[j] = True
                    merged = True
            out.append(cur)
        items = out

    results = []
    for it in items:
        ordered = [t for _, _, t in sorted(it["texts"], key=lambda z: (round(z[0] / 20.0), z[1]))]
        results.append(
            {
                "x1_pct": (it["x1"] / frame_w) * 100.0,
                "y1_pct": (it["y1"] / frame_h) * 100.0,
                "x2_pct": (it["x2"] / frame_w) * 100.0,
                "y2_pct": (it["y2"] / frame_h) * 100.0,
                "jp_text": " ".join(ordered).strip(),
                "conf": it["conf"],
                "angle_deg": it["ang"],
            }
        )
    return results


def scan_japanese_signs(
    video_path: str,
    work_dir: str,
    min_height_pct: float = 2.6,
    skip_op_ed_credits: bool = True,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> List[Dict]:
    """Scan episode frames on GPU to detect silent Japanese signs, extract colors, and save review thumbnails."""
    reader = _get_easyocr_reader(log_cb=log_cb)
    vid_w, vid_h, fps, total_dur = probe_video_info(video_path)
    thumbs_dir = Path(work_dir) / "sign_thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"تعذر فتح ملف الفيديو للفحص البصري: {video_path}")

    step_sec = 0.40
    step_frames = max(1, int(round(fps * step_sec)))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or int(total_dur * fps) or 1)

    scale = min(1.0, 960.0 / max(1, vid_w))
    active_tracks: List[Dict] = []
    finished_tracks: List[Dict] = []

    frame_idx = 0
    last_ui = 0.0
    if log_cb:
        log_cb(f"🔍 بدء فحص اللافتات اليابانية الصامتة ({vid_w}x{vid_h} @ {fps:.2f}fps)...")

    while True:
        if frame_idx > 0:
            for _ in range(step_frames - 1):
                if not cap.grab():
                    break
        ret, frame = cap.read()
        if not ret or frame is None:
            break

        cur_ms = int(round((frame_idx / fps) * 1000.0))
        small = cv2.resize(frame, (int(vid_w * scale), int(vid_h * scale)), interpolation=cv2.INTER_AREA) if scale < 0.99 else frame
        sh, sw = small.shape[:2]

        ocr_out = reader.readtext(small, detail=1, paragraph=False)
        jp_dets = []
        for pts, txt, conf in ocr_out:
            clean_t = (txt or "").strip()
            if conf < 0.34 or not JP_CHAR_RE.search(clean_t):
                continue
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            x1, y1, x2, y2 = max(0.0, min(xs)), max(0.0, min(ys)), min(float(sw), max(xs)), min(float(sh), max(ys))
            h_pct = ((y2 - y1) / max(1.0, float(sh))) * 100.0
            if h_pct < min_height_pct:
                continue
            dx = float(pts[1][0]) - float(pts[0][0])
            dy = float(pts[1][1]) - float(pts[0][1])
            ang = math.degrees(math.atan2(dy, max(1e-4, dx))) if abs(dx) > 4 else 0.0
            if abs(ang) > 35.0:
                ang = 0.0
            jp_dets.append(([x1, y1, x2, y2], clean_t, float(conf), ang))

        # Skip dense credit walls (e.g., >5 distinct text blocks simultaneously during OP/ED)
        if skip_op_ed_credits and len(jp_dets) >= 6:
            merged_boxes = []
        else:
            merged_boxes = _merge_frame_detections(jp_dets, sw, sh)

        matched_track_ids = set()
        for mb in merged_boxes:
            cx = 0.5 * (mb["x1_pct"] + mb["x2_pct"])
            cy = 0.5 * (mb["y1_pct"] + mb["y2_pct"])
            best_tr = None
            for tr in active_tracks:
                tcx = 0.5 * (tr["x1_pct"] + tr["x2_pct"])
                tcy = 0.5 * (tr["y1_pct"] + tr["y2_pct"])
                if abs(cx - tcx) <= 10.0 and abs(cy - tcy) <= 8.5 and (cur_ms - tr["end_ms"]) <= 950:
                    best_tr = tr
                    break

            if best_tr is not None:
                best_tr["end_ms"] = cur_ms + int(step_sec * 500)
                best_tr["x1_pct"] = min(best_tr["x1_pct"], mb["x1_pct"])
                best_tr["y1_pct"] = min(best_tr["y1_pct"], mb["y1_pct"])
                best_tr["x2_pct"] = max(best_tr["x2_pct"], mb["x2_pct"])
                best_tr["y2_pct"] = max(best_tr["y2_pct"], mb["y2_pct"])
                best_tr["hits"] += 1
                if mb["conf"] >= best_tr["best_conf"]:
                    best_tr["best_conf"] = mb["conf"]
                    best_tr["jp_text"] = mb["jp_text"]
                    best_tr["mid_frame"] = frame.copy()
                matched_track_ids.add(id(best_tr))
            else:
                new_tr = {
                    "start_ms": max(0, cur_ms - int(step_sec * 350)),
                    "end_ms": cur_ms + int(step_sec * 500),
                    "x1_pct": mb["x1_pct"],
                    "y1_pct": mb["y1_pct"],
                    "x2_pct": mb["x2_pct"],
                    "y2_pct": mb["y2_pct"],
                    "jp_text": mb["jp_text"],
                    "best_conf": mb["conf"],
                    "angle_deg": mb["angle_deg"],
                    "hits": 1,
                    "first_frame": frame.copy(),
                    "mid_frame": frame.copy(),
                }
                active_tracks.append(new_tr)
                matched_track_ids.add(id(new_tr))

        still_active = []
        for tr in active_tracks:
            if id(tr) not in matched_track_ids and (cur_ms - tr["end_ms"]) > 900:
                finished_tracks.append(tr)
            else:
                still_active.append(tr)
        active_tracks = still_active

        frame_idx += step_frames
        now_t = time.monotonic()
        if progress_cb and (now_t - last_ui >= 0.45):
            last_ui = now_t
            pct = min(98.0, (frame_idx / max(1, total_frames)) * 100.0)
            cm, cs = divmod(cur_ms // 1000, 60)
            tm, ts = divmod(int(total_dur), 60)
            progress_cb(pct, f"فحص بصري OCR · تم رصد {len(finished_tracks) + len(active_tracks)} مرشح", f"{cm:02d}:{cs:02d} / {tm:02d}:{ts:02d}")

    cap.release()
    finished_tracks.extend(active_tracks)

    results: List[Dict] = []
    for tr in finished_tracks:
        dur_ms = tr["end_ms"] - tr["start_ms"]
        # Filter 1-frame OCR glitches (<550ms) and permanent TV station watermarks (>38s in corners)
        if dur_ms < 550:
            continue
        if dur_ms > 38000 and (tr["y2_pct"] < 18.0 or tr["x1_pct"] > 78.0 or tr["x2_pct"] < 22.0):
            continue

        frm = tr["mid_frame"]
        px1 = max(0, int(vid_w * tr["x1_pct"] / 100.0) - 6)
        py1 = max(0, int(vid_h * tr["y1_pct"] / 100.0) - 6)
        px2 = min(vid_w, int(vid_w * tr["x2_pct"] / 100.0) + 6)
        py2 = min(vid_h, int(vid_h * tr["y2_pct"] / 100.0) + 6)

        roi = frm[py1:py2, px1:px2]
        fill_hex, outline_hex, _ = _extract_sign_colors_and_mask(roi)

        # Detect if sign fades in compared to first_frame
        roi_first = tr["first_frame"][py1:py2, px1:px2]
        has_fade = float(np.mean(np.abs(roi.astype(np.float32) - roi_first.astype(np.float32)))) > 16.0

        sign_id = len(results) + 1
        preview_img = frm.copy()
        cv2.rectangle(preview_img, (px1, py1), (px2, py2), (248, 189, 56), max(2, vid_h // 260))
        thumb_w = 480
        thumb_h = max(180, int(round(vid_h * (thumb_w / max(1, vid_w)))))
        thumb_resized = cv2.resize(preview_img, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)
        thumb_file = thumbs_dir / f"sign_{sign_id:03d}.jpg"
        cv2.imwrite(str(thumb_file), thumb_resized, [int(cv2.IMWRITE_JPEG_QUALITY), 88])

        results.append(
            {
                "id": sign_id,
                "enabled": True,
                "start_ms": int(tr["start_ms"]),
                "end_ms": int(tr["end_ms"]),
                "jp_text": tr["jp_text"],
                "ar_text": "",
                "bbox_pct": [
                    round(tr["x1_pct"], 3),
                    round(tr["y1_pct"], 3),
                    round(tr["x2_pct"], 3),
                    round(tr["y2_pct"], 3),
                ],
                "cx_pct": round(0.5 * (tr["x1_pct"] + tr["x2_pct"]), 3),
                "cy_pct": round(0.5 * (tr["y1_pct"] + tr["y2_pct"]), 3),
                "w_pct": round(tr["x2_pct"] - tr["x1_pct"], 3),
                "h_pct": round(tr["y2_pct"] - tr["y1_pct"], 3),
                "fill_hex": fill_hex,
                "outline_hex": outline_hex,
                "has_fade": has_fade,
                "angle_deg": round(float(tr.get("angle_deg", 0.0)), 1),
                "thumb_path": str(thumb_file),
            }
        )

    if progress_cb:
        progress_cb(100.0, f"اكتمل فحص اللافتات: تم العثور على {len(results)} لافتة يابانية ✓", Path(video_path).name)
    if log_cb:
        log_cb(f"✅ اكتمل الفحص البصري: تم استخراج {len(results)} لافتة صامتة جاهزة للمراجعة السريعة.")
    return results


def build_inpainted_sign_patches(
    video_path: str,
    approved_signs: List[Dict],
    patches_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> List[Dict]:
    """Generate clean, flicker-free LaMa-inpainted ROI overlay clips at the video's exact native resolution."""
    active = [s for s in approved_signs if s.get("enabled", True)]
    if not active:
        return []

    vid_w, vid_h, fps, _ = probe_video_info(video_path)
    out_dir = Path(patches_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"تعذر فتح الفيديو لمحو اللافتات: {video_path}")

    patches_meta: List[Dict] = []
    total_signs = len(active)

    for idx, sg in enumerate(active, start=1):
        x1_p, y1_p, x2_p, y2_p = sg["bbox_pct"]
        pad_x = max(14, int(vid_w * 0.018))
        pad_y = max(14, int(vid_h * 0.022))

        # Align ROI coordinates & dimensions to even integers for exact yuv420p chroma alignment
        px1 = max(0, (int(vid_w * x1_p / 100.0) - pad_x) & ~1)
        py1 = max(0, (int(vid_h * y1_p / 100.0) - pad_y) & ~1)
        px2 = min(vid_w & ~1, ((int(vid_w * x2_p / 100.0) + pad_x + 1) & ~1))
        py2 = min(vid_h & ~1, ((int(vid_h * y2_p / 100.0) + pad_y + 1) & ~1))
        pw, ph = px2 - px1, py2 - py1
        if pw < 16 or ph < 16:
            continue

        start_sec = max(0.0, float(sg["start_ms"]) / 1000.0)
        end_sec = max(start_sec + 0.2, float(sg["end_ms"]) / 1000.0)
        start_f = max(0, int(math.floor(start_sec * fps)))
        end_f = max(start_f + 2, int(math.ceil(end_sec * fps)))

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
        crops: List[np.ndarray] = []
        for _ in range(end_f - start_f):
            ok, frm = cap.read()
            if not ok or frm is None:
                break
            crops.append(frm[py1:py2, px1:px2].copy())

        if not crops:
            continue

        # Inner box (where text actually sits inside the padded ROI)
        ix1 = max(2, int(vid_w * x1_p / 100.0) - px1 - 3)
        iy1 = max(2, int(vid_h * y1_p / 100.0) - py1 - 3)
        ix2 = min(pw - 2, int(vid_w * x2_p / 100.0) - px1 + 3)
        iy2 = min(ph - 2, int(vid_h * y2_p / 100.0) - py1 + 3)

        # Check background border motion across frames (Static clean-plate vs Moving scene)
        border_diffs = []
        ref_c = crops[len(crops) // 2].astype(np.float32)
        for c in crops[:: max(1, len(crops) // 6)]:
            cf = c.astype(np.float32)
            ring = np.concatenate(
                [
                    np.abs(cf[:6, :, :] - ref_c[:6, :, :]).reshape(-1),
                    np.abs(cf[-6:, :, :] - ref_c[-6:, :, :]).reshape(-1),
                ]
            )
            border_diffs.append(float(np.mean(ring)))
        is_static_bg = (max(border_diffs) if border_diffs else 0.0) < 5.2

        cleaned_crops: List[np.ndarray] = []
        if is_static_bg:
            mid_crop = crops[len(crops) // 2]
            _, _, stroke_inner = _extract_sign_colors_and_mask(mid_crop[iy1:iy2, ix1:ix2])
            full_mask = np.zeros((ph, pw), dtype=np.uint8)
            full_mask[iy1:iy2, ix1:ix2] = stroke_inner
            clean_plate = _inpaint_patch_lama_or_telea(mid_crop, full_mask, log_cb=log_cb)
            alpha = cv2.GaussianBlur(full_mask.astype(np.float32) / 255.0, (7, 7), 0)[:, :, None]
            for c in crops:
                lum_ratio = float(np.mean(c[:6, :, :]) + 1.0) / float(np.mean(mid_crop[:6, :, :]) + 1.0)
                adj_plate = np.clip(clean_plate.astype(np.float32) * min(1.25, max(0.75, lum_ratio)), 0, 255)
                blended = (alpha * adj_plate + (1.0 - alpha) * c.astype(np.float32)).astype(np.uint8)
                cleaned_crops.append(blended)
        else:
            raw_inpainted: List[np.ndarray] = []
            alphas: List[np.ndarray] = []
            for c in crops:
                _, _, stroke_inner = _extract_sign_colors_and_mask(c[iy1:iy2, ix1:ix2])
                full_mask = np.zeros((ph, pw), dtype=np.uint8)
                full_mask[iy1:iy2, ix1:ix2] = stroke_inner
                inp = _inpaint_patch_lama_or_telea(c, full_mask, log_cb=log_cb)
                raw_inpainted.append(inp)
                alphas.append(cv2.GaussianBlur(full_mask.astype(np.float32) / 255.0, (7, 7), 0)[:, :, None])

            # 3-frame temporal smoothing inside stroke mask to prevent moving-scene flicker
            for f_i in range(len(crops)):
                p0 = raw_inpainted[max(0, f_i - 1)].astype(np.float32)
                p1 = raw_inpainted[f_i].astype(np.float32)
                p2 = raw_inpainted[min(len(crops) - 1, f_i + 1)].astype(np.float32)
                smooth_inp = 0.25 * p0 + 0.50 * p1 + 0.25 * p2
                a = alphas[f_i]
                blended = (a * smooth_inp + (1.0 - a) * crops[f_i].astype(np.float32)).astype(np.uint8)
                cleaned_crops.append(blended)

        patch_file = out_dir / f"patch_{sg['id']:03d}_{vid_h}p.mkv"
        ff_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{pw}x{ph}",
            "-r", f"{fps:.6f}",
            "-i", "pipe:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "12",
            "-pix_fmt", "yuv420p",
            str(patch_file),
        ]
        proc = subprocess.Popen(ff_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdin is not None
        for frm_out in cleaned_crops:
            proc.stdin.write(frm_out.tobytes())
        proc.stdin.close()
        if proc.wait() != 0 or not patch_file.is_file():
            err_t = (proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else "")[-250:]
            raise RuntimeError(f"فشل ترميز رقعة المحو البصري #{sg['id']}: {err_t}")

        actual_start = start_f / fps
        actual_end = (start_f + len(cleaned_crops)) / fps
        patches_meta.append(
            {
                "path": str(patch_file),
                "px": px1,
                "py": py1,
                "start_sec": round(actual_start, 4),
                "end_sec": round(actual_end, 4),
            }
        )

        if progress_cb:
            pct = (idx / max(1, total_signs)) * 100.0
            mode_str = "خلفية ثابتة Clean-Plate" if is_static_bg else "خلفية متحركة Temporal-LaMa"
            progress_cb(pct, f"محو اللافتة {idx}/{total_signs} ({mode_str})", f"{vid_w}x{vid_h}")

    cap.release()
    if log_cb:
        log_cb(f"✨ اكتمل محو {len(patches_meta)} لافتة يابانية بنجاح لجودة {vid_w}x{vid_h}.")
    return patches_meta


def find_completed_video(folder: Path) -> Optional[Path]:
    """Return completed video file (>1MB and no active .aria2 control file) if already downloaded."""
    if not folder.exists():
        return None
    if any(folder.rglob("*.aria2")):
        return None
    videos = sorted(
        [
            p for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and p.stat().st_size > 1024 * 1024
        ],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return videos[0] if videos else None


def download_torrent_or_url(
    source_link: Optional[str],
    torrent_file: Optional[str],
    output_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Path:
    """Download or resume via aria2c into persistent storage, skipping immediately if already cached."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if torrent_file and os.path.isfile(torrent_file) and Path(torrent_file).suffix.lower() in VIDEO_EXTENSIONS:
        vp = Path(torrent_file)
        mb = vp.stat().st_size / (1024 * 1024)
        if log_cb:
            log_cb(f"⚡ استخدام ملف الفيديو المحفوظ مباشرة ({mb:.1f} MB): {vp.name}")
        if progress_cb:
            progress_cb(100.0, "جاهز محلياً بدون تحميل ⚡", f"{mb:.1f} MB · {vp.name}")
        return vp

    cached = find_completed_video(out_path)
    if cached is not None:
        mb = cached.stat().st_size / (1024 * 1024)
        if log_cb:
            log_cb(f"⚡ الحلقة محفوظة مسبقاً بالكامل ({mb:.1f} MB): {cached.name} — تم تخطي التحميل!")
        if progress_cb:
            progress_cb(100.0, "محفوظة مسبقاً على السيرفر ⚡", f"{mb:.1f} MB · {cached.name}")
        return cached

    target = None
    if torrent_file and os.path.isfile(torrent_file):
        target = torrent_file
    elif source_link and source_link.strip():
        target = source_link.strip()

    if not target:
        raise ValueError("لم يتم إدخال رابط أو اختيار حلقة محفوظة.")

    has_partial = any(out_path.rglob("*.aria2"))
    overwrite_flag = "--allow-overwrite=false" if has_partial else "--allow-overwrite=true"

    cmd = [
        "aria2c",
        "--dir", output_dir,
        "--continue=true",
        overwrite_flag,
        "--auto-file-renaming=false",
        "--seed-time=0",
        "--bt-stop-timeout=300",
        "--max-connection-per-server=8",
        "--split=8",
        "--file-allocation=none",
        "--summary-interval=1",
        target,
    ]
    if log_cb:
        mode_txt = "استكمال التحميل السابق" if has_partial else "بدء التحميل وحفظه دائماً"
        log_cb(f"[aria2c] {mode_txt} في: {output_dir}")
    if progress_cb:
        progress_cb(1.0, "جاري الاتصال بالمصدر وجلب البيانات...", output_dir)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None

    last_log_t = 0.0
    for line in proc.stdout:
        clean = line.strip()
        if not clean:
            continue
        pct_m = re.search(r"\((\d+(?:\.\d+)?)%\)", clean)
        if pct_m:
            pct = float(pct_m.group(1))
            dl_m = re.search(r"DL:([^\s\]]+)", clean)
            eta_m = re.search(r"ETA:([^\s\]]+)", clean)
            sz_m = re.search(r"([\d.]+\w*B/[\d.]+\w*B)", clean)
            dl_spd = dl_m.group(1) if dl_m else "—"
            eta_str = eta_m.group(1) if eta_m else "—"
            sz_str = sz_m.group(1) if sz_m else ""
            if progress_cb:
                progress_cb(pct, f"السرعة: {dl_spd}/s · المتبقي: {eta_str}", sz_str or "جاري التحميل...")
            now_t = time.monotonic()
            if log_cb and (now_t - last_log_t >= 6.0):
                last_log_t = now_t
                log_cb(f"[aria2c] {pct:.0f}% | السرعة: {dl_spd}/s | الحجم: {sz_str} | المتبقي: {eta_str}")
        elif "METADATA" in clean.upper() and progress_cb:
            progress_cb(2.0, "جاري سحب بيانات التورنت (Metadata)...", "الاتصال بالـ Seeders")
        elif log_cb and any(k in clean for k in ("Download complete", "ERROR")):
            log_cb(f"[aria2c] {clean}")

    if proc.wait() != 0:
        raise RuntimeError("فشل تحميل aria2c — تأكد من صحة رابط التورنت أو الرابط المباشر.")

    videos = sorted(
        [p for p in out_path.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not videos:
        raise FileNotFoundError(f"لم يتم العثور على ملف فيديو داخل {output_dir}")

    mb = videos[0].stat().st_size / (1024 * 1024)
    if progress_cb:
        progress_cb(100.0, "اكتمل التحميل 100% وحُفظ دائماً ✓", f"{mb:.1f} MB · {videos[0].name}")
    return videos[0]


def _escape_ffmpeg_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _has_nvenc(codec: str) -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return codec in (res.stdout or "")


def _parse_hms(val: str) -> float:
    try:
        parts = val.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        pass
    return 0.0


def burn_and_compress_native(
    video_path: str,
    ass_path: str,
    output_path: str,
    fonts_dir: str,
    codec: str = "h264_nvenc",
    cq: int = 23,
    preset: str = "p6",
    audio_bitrate: str = "160k",
    sign_patches: Optional[List[Dict]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """Single-pass NVENC burn: composites clean LaMa sign patches (if any) + responsive ASS subtitles."""
    verify_required_fonts(fonts_dir)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ass_filter = f"ass='{_escape_ffmpeg_path(os.path.abspath(ass_path))}':fontsdir='{_escape_ffmpeg_path(os.path.abspath(fonts_dir))}'"

    if codec.endswith("_nvenc") and _has_nvenc(codec):
        v_args = [
            "-c:v", codec, "-preset", preset, "-rc", "vbr",
            "-cq", str(cq), "-b:v", "0",
            "-spatial-aq", "1", "-temporal-aq", "1", "-pix_fmt", "yuv420p",
        ]
        if codec == "hevc_nvenc":
            v_args += ["-tag:v", "hvc1"]
    else:
        cpu_codec = "libx265" if "hevc" in codec else "libx264"
        if log_cb:
            log_cb(f"NVENC unavailable; falling back to {cpu_codec}.")
        v_args = ["-c:v", cpu_codec, "-preset", "medium", "-crf", str(cq), "-pix_fmt", "yuv420p"]
        if cpu_codec == "libx265":
            v_args += ["-tag:v", "hvc1"]

    _, _, _, total_dur = probe_video_info(video_path)
    patches = sign_patches or []

    if not patches:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vf", ass_filter, *v_args,
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            output_path,
        ]
    else:
        in_args = ["-i", video_path]
        fc_parts = []
        prev_lbl = "[0:v]"
        for idx, pm in enumerate(patches, start=1):
            in_args.extend(["-i", pm["path"]])
            st_s = pm["start_sec"]
            en_s = pm["end_sec"]
            fc_parts.append(f"[{idx}:v]setpts=PTS+{st_s:.4f}/TB[sp{idx}]")
            next_lbl = f"[ov{idx}]"
            fc_parts.append(
                f"{prev_lbl}[sp{idx}]overlay=x={pm['px']}:y={pm['py']}:eof_action=pass:enable='between(t,{st_s:.4f},{en_s:.4f})'{next_lbl}"
            )
            prev_lbl = next_lbl
        fc_parts.append(f"{prev_lbl}{ass_filter}[vfinal]")
        filter_complex = ";".join(fc_parts)

        cmd = [
            "ffmpeg", "-y", *in_args,
            "-filter_complex", filter_complex,
            "-map", "[vfinal]", "-map", "0:a:0?",
            *v_args,
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            output_path,
        ]

    if log_cb:
        log_cb(f"[ffmpeg] بدء الحرق والضغط لـ {Path(output_path).name} ({v_args[1]}, رقع ممسوحة={len(patches)})...")
    if progress_cb:
        progress_cb(0.5, f"تشغيل محرك {v_args[1]}...", Path(output_path).name)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None

    PROGRESS_KEYS = (
        "frame=", "fps=", "stream_", "bitrate=", "total_size=",
        "out_time", "dup_frames=", "drop_frames=", "speed=", "progress=",
    )
    tail: List[str] = []
    pstate: Dict[str, str] = {}
    last_log_t = 0.0

    for line in proc.stdout:
        clean = line.strip()
        if not clean:
            continue
        if clean.startswith(PROGRESS_KEYS) and "=" in clean:
            k, v = clean.split("=", 1)
            pstate[k.strip()] = v.strip()
            if k.strip() == "progress":
                cur_sec = _parse_hms(pstate.get("out_time", "00:00:00"))
                fps_v = pstate.get("fps", "0")
                spd_v = pstate.get("speed", "—")
                sz_mb = int(pstate.get("total_size", "0") or "0") / (1024 * 1024)
                pct = min(99.8, (cur_sec / total_dur) * 100.0) if total_dur > 0 else 50.0
                spd_num = 0.0
                try:
                    spd_num = float(spd_v.replace("x", "").strip())
                except Exception:
                    pass
                eta_txt = "—"
                if total_dur > 0 and spd_num > 0.05:
                    rem = max(0, int((total_dur - cur_sec) / spd_num))
                    eta_txt = f"{rem // 60:02d}:{rem % 60:02d}"
                cm, cs = divmod(int(cur_sec), 60)
                tm, ts = divmod(int(total_dur), 60)
                if progress_cb:
                    progress_cb(
                        pct,
                        f"السرعة: {spd_v} · {fps_v} fps · المتبقي: {eta_txt}",
                        f"{cm:02d}:{cs:02d} / {tm:02d}:{ts:02d} · {sz_mb:.1f} MB",
                    )
                now_t = time.monotonic()
                if log_cb and (now_t - last_log_t >= 8.0):
                    last_log_t = now_t
                    log_cb(f"[ffmpeg] {pct:.1f}% | السرعة: {spd_v} | الحجم: {sz_mb:.1f} MB | المتبقي: {eta_txt}")
        else:
            tail.append(clean)
            if len(tail) > 25:
                tail.pop(0)

    if proc.wait() != 0:
        raise RuntimeError("FFmpeg failed:\n" + "\n".join(tail[-10:]))

    orig_mb = os.path.getsize(video_path) / (1024 * 1024)
    new_mb = os.path.getsize(output_path) / (1024 * 1024)
    if progress_cb:
        progress_cb(100.0, "اكتمل الحرق والضغط 100% ✓", f"{orig_mb:.1f} MB → {new_mb:.1f} MB")
    if log_cb:
        log_cb(f"[ffmpeg] اكتمل بنجاح: {orig_mb:.1f} MB -> {new_mb:.1f} MB ({output_path})")
    return output_path


def upload_to_rclone(
    local_file: str,
    remote_name: str,
    folder_path: str = "Runpod_Anime",
    generate_link: bool = True,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    fname = os.path.basename(local_file)
    dest_dir = f"{remote_name.strip().rstrip(':')}:{folder_path.strip().strip('/')}"
    dest_file = f"{dest_dir}/{fname}"
    if progress_cb:
        progress_cb(1.0, f"بدء الرفع إلى {remote_name}...", fname)

    proc = subprocess.Popen(
        ["rclone", "copy", local_file, dest_dir, "--stats=1s", "--stats-one-line", "-v"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    tail: List[str] = []
    last_log_t = 0.0
    for line in proc.stdout:
        clean = line.strip()
        if not clean:
            continue
        tail.append(clean)
        if len(tail) > 15:
            tail.pop(0)
        pct_m = re.search(r"(\d+(?:\.\d+)?)%", clean)
        if pct_m:
            pct = float(pct_m.group(1))
            spd_m = re.search(r"([\d.]+\s*\w+B/s)", clean)
            eta_m = re.search(r"ETA\s*([^\s,]+)", clean)
            spd = spd_m.group(1) if spd_m else "—"
            eta = eta_m.group(1) if eta_m else "—"
            if progress_cb:
                progress_cb(pct, f"السرعة: {spd} · المتبقي: {eta}", fname)
            now_t = time.monotonic()
            if log_cb and (now_t - last_log_t >= 6.0):
                last_log_t = now_t
                log_cb(f"[rclone:{remote_name}] {pct:.0f}% | {spd} | ETA {eta}")

    if proc.wait() != 0:
        raise RuntimeError(f"Rclone upload to {remote_name} failed:\n" + "\n".join(tail[-6:]))

    if progress_cb:
        progress_cb(100.0, f"اكتمل الرفع على {remote_name} ✓", fname)

    if generate_link:
        link_res = subprocess.run(["rclone", "link", dest_file], capture_output=True, text=True)
        if link_res.returncode == 0 and link_res.stdout.strip():
            return link_res.stdout.strip()
    return dest_file


def upload_to_doodstream(
    local_file: str,
    api_key: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Dict[str, str]:
    key = api_key.strip()
    if not key:
        raise ValueError("يرجى إدخال مفتاح DoodStream API Key.")

    req = urllib.request.Request(f"https://doodapi.co/api/upload/server?key={key}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        server_data = json.loads(resp.read().decode("utf-8"))
    if server_data.get("status") != 200 or not server_data.get("result"):
        raise RuntimeError(f"DoodStream server error: {server_data}")

    upload_url = f"{server_data['result']}?{key}"
    fname = os.path.basename(local_file)
    if log_cb:
        log_cb(f"[DoodStream] جاري رفع {fname}...")
    if progress_cb:
        progress_cb(1.0, "جاري الرفع على سيرفر DoodStream...", fname)

    proc = subprocess.Popen(
        ["curl", "--no-buffer", "-#", "-X", "POST", "-F", f"api_key={key}", "-F", f"file=@{local_file}", upload_url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out_chunks: List[bytes] = []

    def _collect_stdout() -> None:
        assert proc.stdout is not None
        while True:
            b = proc.stdout.read(4096)
            if not b:
                break
            out_chunks.append(b)

    t_out = threading.Thread(target=_collect_stdout, daemon=True)
    t_out.start()

    assert proc.stderr is not None
    buf = ""
    last_pct = -1.0
    while True:
        chunk = proc.stderr.read(128)
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="ignore")
        parts = re.split(r"[\r\n]+", buf)
        buf = parts[-1]
        for seg in parts[:-1]:
            m = re.search(r"(\d+(?:\.\d+)?)%", seg)
            if m:
                pct = float(m.group(1))
                if pct - last_pct >= 0.5 or pct >= 100.0:
                    last_pct = pct
                    if progress_cb:
                        progress_cb(pct, "رفع مباشر إلى DoodStream", fname)

    t_out.join()
    if proc.wait() != 0:
        raise RuntimeError(f"DoodStream curl error: {buf[-300:]}")

    raw_json = b"".join(out_chunks).decode("utf-8", errors="replace")
    data = json.loads(raw_json or "{}")
    if data.get("status") != 200 or not data.get("result"):
        raise RuntimeError(f"DoodStream API error: {data}")

    if progress_cb:
        progress_cb(100.0, "اكتمل الرفع على DoodStream ✓", fname)

    item = data["result"][0] if isinstance(data["result"], list) else data["result"]
    code = item.get("filecode", "")
    return {
        "filecode": code,
        "watch_url": item.get("download_url") or f"https://doodstream.com/d/{code}",
        "embed_url": item.get("protected_embed") or f"https://doodstream.com/e/{code}",
    }
