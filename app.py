import hashlib
import html
import inspect
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import gradio as gr

from media_ops import (
    VIDEO_EXTENSIONS,
    build_inpainted_sign_patches,
    burn_and_compress_native,
    download_torrent_or_url,
    probe_video_info,
    scan_japanese_signs,
    upload_to_doodstream,
    upload_to_rclone,
)
from subtitles_ai import (
    ensure_style_profile,
    extract_embedded_subtitle,
    load_or_extract_series_glossary,
    parse_source_subtitles,
    probe_embedded_subtitles,
    save_series_glossary,
    transcribe_with_whisper,
    translate_detected_signs,
    translate_entries,
    write_responsive_ass,
)

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"
STORAGE_ROOT = Path(os.environ.get("RUNPOD_OUTPUT_ROOT", "/workspace/outputs"))
RAW_CACHE_ROOT = Path(os.environ.get("RUNPOD_RAW_CACHE", "/workspace/raw_episodes"))
LEGACY_TEMP_ROOT = Path(os.environ.get("RUNPOD_TEMP_ROOT", "/workspace/tmp_downloads"))
SETTINGS_FILE = Path(os.environ.get("RUNPOD_SETTINGS_FILE", "/workspace/subber_settings.json"))

ensure_style_profile()

SHADOW_BLADE_HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@500;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
"""

SHADOW_BLADE_CSS = """
:root {
    --sb-void: #040710;
    --sb-deep: #080E1E;
    --sb-panel: #0B1326;
    --sb-panel-alt: #0F1A33;
    --sb-cyan: #38BDF8;
    --sb-cyan-bright: #7DD3FC;
    --sb-violet: #A78BFA;
    --sb-amber: #FBBF24;
    --sb-text: #F1F5F9;
    --sb-muted: #94A3B8;
    --sb-success: #34D399;
    --sb-danger: #FB7185;
}

html, body, .gradio-container {
    background-color: var(--sb-void) !important;
    background-image:
        radial-gradient(circle at 14% 10%, rgba(56, 189, 248, 0.09) 0%, transparent 40%),
        radial-gradient(circle at 86% 16%, rgba(167, 139, 250, 0.09) 0%, transparent 42%),
        linear-gradient(180deg, #040710 0%, #060B18 100%) !important;
    color: var(--sb-text) !important;
    font-family: 'Cairo', sans-serif !important;
    direction: rtl !important;
}

.gradio-container {
    max-width: 1520px !important;
    margin: 0 auto !important;
    padding: 16px 20px 36px !important;
}

#sb-global-vfx-canvas {
    position: fixed;
    inset: 0;
    width: 100vw;
    height: 100vh;
    pointer-events: none;
    z-index: 9998;
}

.sb-hero-wrap {
    position: relative;
    background: linear-gradient(135deg, rgba(11, 19, 38, 0.96) 0%, rgba(7, 12, 26, 0.98) 100%);
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-right: 4px solid var(--sb-cyan);
    border-radius: 6px;
    padding: 18px 22px;
    margin-bottom: 18px;
    box-shadow: 0 10px 32px rgba(0, 0, 0, 0.55);
}

.sb-hero-inner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    flex-wrap: wrap;
}

.sb-brand-side {
    display: flex;
    align-items: center;
    gap: 16px;
}

.sb-emblem-box {
    width: 60px;
    height: 60px;
    background: #060B18;
    border: 1px solid rgba(56, 189, 248, 0.45);
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
}

.sb-title-eyebrow {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--sb-cyan);
}

.sb-main-title {
    font-size: 22px;
    font-weight: 900;
    color: #F8FAFC;
    margin: 2px 0;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
}

.sb-rank-pill {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 700;
    padding: 2px 9px;
    background: rgba(167, 139, 250, 0.14);
    color: var(--sb-violet);
    border: 1px solid rgba(167, 139, 250, 0.45);
    border-radius: 3px;
}

.sb-subtitle-desc {
    margin: 4px 0 0;
    font-size: 13px;
    color: var(--sb-muted);
    font-weight: 600;
}

.sb-hud-chip {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    color: #CBD5E1;
    background: rgba(15, 26, 51, 0.85);
    border: 1px solid rgba(148, 163, 184, 0.22);
    padding: 6px 11px;
    border-radius: 4px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
}

.sb-dot-online {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--sb-success);
    box-shadow: 0 0 8px var(--sb-success);
    display: inline-block;
}

.gradio-container .block,
.gradio-container .form,
.gradio-container .gr-box,
.gradio-container .gr-panel,
.sb-accordion {
    background-color: var(--sb-panel) !important;
    border-color: rgba(56, 189, 248, 0.2) !important;
    border-radius: 6px !important;
}

.gradio-container input[type="text"],
.gradio-container input[type="password"],
.gradio-container input[type="number"],
.gradio-container textarea,
.gradio-container select {
    background-color: #050A16 !important;
    color: #F8FAFC !important;
    border: 1px solid rgba(56, 189, 248, 0.24) !important;
    border-radius: 4px !important;
    font-family: 'Cairo', 'JetBrains Mono', sans-serif !important;
    font-size: 13.5px !important;
    unicode-bidi: plaintext !important;
}

#sb-scan-btn {
    min-height: 56px !important;
    background: linear-gradient(135deg, #1E1B0A 0%, #0F172A 100%) !important;
    color: #FEF3C7 !important;
    border: 1.5px solid var(--sb-amber) !important;
    font-family: 'Cairo', sans-serif !important;
    font-size: 15.5px !important;
    font-weight: 900 !important;
    border-radius: 6px !important;
    box-shadow: 0 0 20px rgba(251, 191, 36, 0.18) !important;
    margin: 8px 0 !important;
}

#sb-step1-btn, #sb-step2-btn {
    min-height: 64px !important;
    border-radius: 6px !important;
    font-family: 'Cairo', sans-serif !important;
    font-size: 16.5px !important;
    font-weight: 900 !important;
    margin: 8px 0 !important;
}

#sb-step1-btn {
    background: #061024 !important;
    color: #F8FAFC !important;
    border: 1.5px solid var(--sb-cyan) !important;
    box-shadow: 0 0 24px rgba(56, 189, 248, 0.25) !important;
}

#sb-step2-btn {
    background: #0D0924 !important;
    color: #F8FAFC !important;
    border: 1.5px solid var(--sb-violet) !important;
    box-shadow: 0 0 24px rgba(167, 139, 250, 0.24) !important;
}

.sb-prog-card {
    background: linear-gradient(135deg, rgba(11, 19, 38, 0.98) 0%, rgba(5, 10, 22, 0.99) 100%);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-right: 4px solid var(--sb-cyan);
    border-radius: 6px;
    padding: 14px 18px;
    margin: 10px 0 12px;
    direction: rtl;
}
.sb-prog-card.sb-mode-violet { border-color: rgba(167, 139, 250, 0.42); border-right-color: var(--sb-violet); }
.sb-prog-card.sb-mode-done { border-color: rgba(52, 211, 153, 0.45); border-right-color: var(--sb-success); }
.sb-prog-card.sb-mode-error { border-color: rgba(251, 113, 133, 0.5); border-right-color: var(--sb-danger); }
.sb-prog-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.sb-prog-badge {
    font-family: 'JetBrains Mono', 'Cairo', monospace;
    font-size: 11.5px; font-weight: 800; color: var(--sb-cyan-bright);
    background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.32);
    padding: 2px 9px; border-radius: 4px; display: inline-block; margin-bottom: 4px;
}
.sb-prog-title { font-size: 14.5px; font-weight: 800; color: #F8FAFC; }
.sb-prog-pct { font-family: 'JetBrains Mono', monospace; font-size: 27px; font-weight: 900; color: var(--sb-cyan-bright); direction: ltr; }
.sb-prog-track { width: 100%; height: 11px; background: #03060E; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 999px; overflow: hidden; }
.sb-prog-fill { height: 100%; background: linear-gradient(90deg, #38BDF8 0%, #818CF8 60%, #A78BFA 100%); border-radius: 999px; transition: width 0.3s ease; }
.sb-prog-meta { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 8px; font-size: 12px; color: var(--sb-muted); font-family: 'JetBrains Mono', 'Cairo', monospace; flex-wrap: wrap; }
"""

SHADOW_BLADE_HERO_HTML = """
<div class="sb-hero-wrap">
    <div class="sb-hero-inner">
        <div class="sb-brand-side">
            <div class="sb-emblem-box">
                <svg viewBox="0 0 64 64" width="42" height="42" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <polygon points="32,4 58,18 58,46 32,60 6,46 6,18" stroke="#38BDF8" stroke-width="1.6" fill="#070D1D"/>
                    <path d="M16 48 L47 15 L51 19 L22 50 Z" fill="#38BDF8"/>
                    <path d="M48 48 L17 15 L13 19 L42 50 Z" fill="#A78BFA" fill-opacity="0.9"/>
                    <circle cx="32" cy="32" r="4.5" fill="#F8FAFC"/>
                </svg>
            </div>
            <div>
                <div class="sb-title-eyebrow">CELESTIAL ENGINE · DEEPSEEK V4.1 · LAMA INPAINTING</div>
                <h1 class="sb-main-title">
                    نصل الظل · سَبّر الأنمي الاحترافي
                    <span class="sb-rank-pill">QUICK REVIEW · %W/%H RESPONSIVE</span>
                </h1>
                <p class="sb-subtitle-desc">
                    <strong>1 [SCAN]:</strong> فحص اللافتات اليابانية الصامتة والقاموس ومراجعتها بضغطة &nbsp;←&nbsp;
                    <strong>2 [STRIKE]:</strong> محو الياباني بـ LaMa وترجمة Celestial-Dragons وحرق كل الجودات بالنسب المئوية &nbsp;←&nbsp;
                    <strong>3 [S-PORTAL]:</strong> الرفع السحابي
                </p>
            </div>
        </div>
        <div class="sb-hud-chip">
            <span class="sb-dot-online"></span>
            <span>DEEPSEEK-FLASH · LAMA GPU · WHISPER · NVENC</span>
        </div>
    </div>
</div>
"""


def _rescue_legacy_tmp_downloads() -> None:
    RAW_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if not LEGACY_TEMP_ROOT.is_dir():
        return
    for vid in LEGACY_TEMP_ROOT.rglob("*"):
        if not vid.is_file() or vid.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if vid.stat().st_size < 1024 * 1024 or any(vid.parent.glob("*.aria2")):
            continue
        dest_dir = RAW_CACHE_ROOT / f"rescued_{vid.parent.name}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / vid.name
        if not dest_file.exists():
            try:
                shutil.move(str(vid), str(dest_file))
            except Exception:
                pass


_rescue_legacy_tmp_downloads()


def render_progress_hud(
    pct: float = 0.0,
    badge: str = "HUNTER HUD · STANDBY",
    title: str = "جاهز للانطلاق — افحص اللافتات [SCAN] للمراجعة السريعة أو اضغط [STRIKE] مباشرة",
    metric: str = "السرعة: — · المتبقي: —",
    detail: str = "الحلقات الخام والقواميس والموديلات محفوظة دائماً في /workspace",
    mode: str = "cyan",
) -> str:
    clamped = max(0.0, min(100.0, float(pct)))
    mode_cls = {"violet": "sb-mode-violet", "done": "sb-mode-done", "error": "sb-mode-error"}.get(mode, "")
    return f"""
    <div class="sb-prog-card {mode_cls}">
        <div class="sb-prog-top">
            <div>
                <div class="sb-prog-badge">{html.escape(badge)}</div>
                <div class="sb-prog-title">{html.escape(title)}</div>
            </div>
            <div class="sb-prog-pct">{clamped:.1f}%</div>
        </div>
        <div class="sb-prog-track">
            <div class="sb-prog-fill" style="width: {clamped:.1f}%;"></div>
        </div>
        <div class="sb-prog-meta">
            <span>⚡ {html.escape(metric)}</span>
            <span>📦 {html.escape(detail)}</span>
        </div>
    </div>
    """


def load_settings() -> Dict:
    defaults = {
        "provider": "DeepSeek API (موصى به)",
        "llm_model": "deepseek-flash",
        "api_key": os.environ.get("DEEPSEEK_API_KEY", os.environ.get("GEMINI_API_KEY", "")),
        "base_url": "https://api.deepseek.com",
        "style_preset": "black_clover_style",
        "dood_api_key": os.environ.get("DOODSTREAM_API_KEY", ""),
        "mega_remote": "mega",
        "mega_folder": "Runpod_Anime",
        "gdrive_remote": "gdrive",
        "gdrive_folder": "Runpod_Anime",
        "codec": "h264_nvenc",
        "preset": "p6",
        "cq": 23,
        "audio_bitrate": "160k",
        "credit_translator": "",
        "credit_typesetter": "",
        "credit_encoder": "",
    }
    if SETTINGS_FILE.is_file():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update({k: v for k, v in saved.items() if v is not None})
        except Exception:
            pass
    return defaults


def save_settings(
    provider, llm_model, api_key, base_url, style_preset, dood_api_key,
    mega_remote, mega_folder, gdrive_remote, gdrive_folder,
    codec, preset, cq, audio_bitrate,
    credit_translator, credit_typesetter, credit_encoder,
) -> str:
    data = {
        "provider": provider,
        "llm_model": llm_model,
        "api_key": api_key,
        "base_url": base_url,
        "style_preset": style_preset,
        "dood_api_key": dood_api_key,
        "mega_remote": mega_remote,
        "mega_folder": mega_folder,
        "gdrive_remote": gdrive_remote,
        "gdrive_folder": gdrive_folder,
        "codec": codec,
        "preset": preset,
        "cq": int(cq),
        "audio_bitrate": audio_bitrate,
        "credit_translator": credit_translator,
        "credit_typesetter": credit_typesetter,
        "credit_encoder": credit_encoder,
    }
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"✅ تم حفظ جميع المفاتيح والإعدادات الدائمة في `{SETTINGS_FILE}`."


def _on_provider_change(prov: str) -> Tuple[Any, Any]:
    if "DeepSeek" in prov:
        return gr.update(value="deepseek-flash"), gr.update(value="https://api.deepseek.com")
    if "Gemini" in prov:
        return gr.update(value="gemini-3.8-flash"), gr.update(value="")
    return gr.update(value="deepseek-flash"), gr.update(value="http://localhost:8000/v1")


def _provider_key(prov_label: str) -> str:
    if "Gemini" in (prov_label or ""):
        return "gemini"
    if "DeepSeek" in (prov_label or ""):
        return "deepseek"
    return "openai"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", (name or "").strip())
    return cleaned or time.strftime("Episode_%Y%m%d_%H%M%S")


def _ms_fmt(ms: int) -> str:
    s_tot = max(0.0, int(ms) / 1000.0)
    m = int(s_tot // 60)
    s = s_tot - m * 60
    return f"{m:02d}:{s:05.2f}"


def _is_truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val or "").strip().lower()
    return s in ("true", "1", "yes", "y", "نعم", "✓", "on")


def _signs_to_table_and_gallery(signs: List[Dict]) -> Tuple[List[List[Any]], List[Tuple[str, str]]]:
    rows: List[List[Any]] = []
    gallery: List[Tuple[str, str]] = []
    for sg in signs:
        rows.append(
            [
                bool(sg.get("enabled", True)),
                int(sg["id"]),
                _ms_fmt(sg["start_ms"]),
                _ms_fmt(sg["end_ms"]),
                sg.get("jp_text", ""),
                sg.get("ar_text", ""),
                sg.get("fill_hex", "#FFFFFF"),
                sg.get("outline_hex", "#000000"),
            ]
        )
        tp = sg.get("thumb_path", "")
        if tp and os.path.isfile(tp):
            cap = f"#{sg['id']} [{_ms_fmt(sg['start_ms'])}] {sg.get('ar_text') or sg.get('jp_text', '')}"
            gallery.append((tp, cap))
    return rows, gallery


def _merge_table_edits_into_signs(orig_signs: List[Dict], table_rows: Any) -> List[Dict]:
    if not orig_signs or table_rows is None:
        return orig_signs
    if hasattr(table_rows, "values"):
        rows_list = table_rows.values.tolist()
    elif isinstance(table_rows, list):
        rows_list = table_rows
    else:
        return orig_signs

    by_id = {int(s["id"]): dict(s) for s in orig_signs}
    for r in rows_list:
        if not r or len(r) < 6:
            continue
        try:
            sid = int(float(r[1]))
        except Exception:
            continue
        if sid not in by_id:
            continue
        by_id[sid]["enabled"] = _is_truthy(r[0])
        by_id[sid]["ar_text"] = str(r[5] or "").strip()
        if len(r) > 6 and str(r[6] or "").strip():
            by_id[sid]["fill_hex"] = str(r[6]).strip()
        if len(r) > 7 and str(r[7] or "").strip():
            by_id[sid]["outline_hex"] = str(r[7]).strip()
    return [by_id[k] for k in sorted(by_id.keys())]


def list_local_outputs() -> List[str]:
    if not STORAGE_ROOT.exists():
        return []
    mp4s = sorted(STORAGE_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in mp4s[:50]]


def list_cached_raw_videos() -> List[str]:
    _rescue_legacy_tmp_downloads()
    if not RAW_CACHE_ROOT.exists():
        return []
    vids = [
        p for p in RAW_CACHE_ROOT.rglob("*")
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTENSIONS
        and "_work" not in p.parts
        and p.stat().st_size > 1024 * 1024
        and not any(p.parent.glob("*.aria2"))
    ]
    vids.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in vids[:60]]


def _raw_cache_dir_for_source(q_label: str, source_link: Optional[str], torrent_file: Optional[str], ep_name: str) -> Path:
    RAW_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if source_link and source_link.strip():
        s = source_link.strip()
        m_hash = re.search(r"btih:([a-zA-Z0-9]+)", s, re.IGNORECASE)
        key = f"magnet_{m_hash.group(1).lower()[:16]}" if m_hash else f"url_{hashlib.sha1(s.encode('utf-8')).hexdigest()[:14]}"
    elif torrent_file and os.path.isfile(torrent_file):
        h = hashlib.sha1(Path(torrent_file).read_bytes()).hexdigest()[:14]
        key = f"tor_{h}"
    else:
        key = _safe_name(ep_name)
    return RAW_CACHE_ROOT / f"{key}_{q_label}"


def step0_quick_scan_signs_and_glossary(
    episode_name: str,
    series_name: str,
    saved_raw_1080: str,
    link_1080: str, file_1080: Optional[str],
    link_720: str, file_720: Optional[str],
    link_480: str, file_480: Optional[str],
    sub_mode: str,
    whisper_model: str,
    min_sign_h_pct: float,
    skip_op_ed_signs: bool,
    provider: str,
    api_key: str,
    llm_model: str,
    base_url: str,
):
    logs: List[str] = []

    def push(msg: str) -> None:
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def pack(hud_html: str, gal=gr.update(), tbl=gr.update(), gls=gr.update(), raw_dd=gr.update(), st=gr.update()):
        return (hud_html, "\n".join(logs[-150:]), gal, tbl, gls, raw_dd, st)

    def run_live(fn: Callable, *args, badge: str, title: str, accepts_progress_cb: bool = True, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        if accepts_progress_cb:
            kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except Exception as e:
                res_box["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        cur_pct, cur_met, cur_det = 1.0, "جاري التنفيذ...", ""
        dirty = True
        last_y = 0.0
        while t.is_alive() or not ev_q.empty():
            try:
                items = [ev_q.get(timeout=0.25)]
            except queue.Empty:
                items = []
            while not ev_q.empty():
                try:
                    items.append(ev_q.get_nowait())
                except queue.Empty:
                    break
            for k, p in items:
                dirty = True
                if k == "prog":
                    cur_pct, cur_met, cur_det = p  # type: ignore[misc]
                else:
                    msg_s = str(p)
                    if msg_s.startswith("[whisper_prog]"):
                        parts = msg_s.replace("[whisper_prog]", "").strip().split("|")
                        try:
                            cur_pct = float(parts[0].replace("%", "").strip())
                        except Exception:
                            pass
                        cur_met = "تفريغ صوتي Faster-Whisper ⚡"
                        cur_det = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        push(msg_s)
            now_m = time.monotonic()
            if dirty and (now_m - last_y >= 0.35 or not t.is_alive()):
                last_y = now_m
                dirty = False
                yield pack(render_progress_hud(cur_pct, badge, title, cur_met, cur_det))
        if "err" in res_box:
            raise res_box["err"]  # type: ignore[misc]
        return res_box.get("val")

    eff_1080 = saved_raw_1080 if (saved_raw_1080 and os.path.isfile(saved_raw_1080)) else file_1080
    q_inputs = [
        (lbl, lnk, fl)
        for lbl, lnk, fl in [("1080p", link_1080, eff_1080), ("720p", link_720, file_720), ("480p", link_480, file_480)]
        if (lnk and lnk.strip()) or fl
    ]
    if not q_inputs:
        push("⚠️ اختر حلقة خام أو أدخل رابط التورنت أولاً قبل الفحص السريع.")
        yield pack(render_progress_hud(0.0, "SCAN · NO SOURCE", "اختر حلقة أو أدخل رابط أولاً", "—", "—", mode="error"))
        return

    auto_base = episode_name.strip() or (Path(eff_1080).stem if (eff_1080 and os.path.isfile(eff_1080)) else "")
    ep_folder_name = _safe_name(auto_base)
    work_dir = RAW_CACHE_ROOT / "_work" / ep_folder_name
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        q_label, lnk, fl = q_inputs[0]
        cdir = _raw_cache_dir_for_source(q_label, lnk, fl, ep_folder_name)
        primary_vid = yield from run_live(
            download_torrent_or_url,
            lnk, fl, str(cdir),
            badge=f"SCAN 1/3 · SOURCE [{q_label}]",
            title="تجهيز الحلقة الخام للفحص البصري...",
            accepts_progress_cb=True,
        )

        source_sub = None
        is_raw_whisper = False
        if sub_mode.startswith("Auto"):
            probe = probe_embedded_subtitles(str(primary_vid))
            if probe is not None:
                source_sub = extract_embedded_subtitle(str(primary_vid), probe[0], probe[1], str(work_dir))
                push(f"✓ تم سحب الترجمة المدمجة ({probe[1]}) لاستخراج السياق والقاموس.")

        if source_sub is None:
            is_raw_whisper = True
            source_sub = yield from run_live(
                transcribe_with_whisper,
                str(primary_vid),
                str(work_dir / "whisper.srt"),
                model_size=whisper_model,
                badge=f"SCAN 2/3 · WHISPER [{whisper_model}]",
                title="فيديو خام بدون ترجمة — جاري التفريغ الصوتي بـ Faster-Whisper...",
                accepts_progress_cb=False,
            )

        entries = parse_source_subtitles(source_sub)

        signs = yield from run_live(
            scan_japanese_signs,
            str(primary_vid),
            str(work_dir),
            min_height_pct=float(min_sign_h_pct),
            skip_op_ed_credits=bool(skip_op_ed_signs),
            badge="SCAN 3/3 · JAPANESE OCR",
            title="فحص كوادر الحلقة بـ EasyOCR للقط اللافتات الصامتة وألوانها...",
            accepts_progress_cb=True,
        )

        glossary = {}
        if api_key.strip() or "Local" in provider:
            glossary = yield from run_live(
                load_or_extract_series_glossary,
                series_name=series_name,
                entries=entries,
                is_raw_whisper=is_raw_whisper,
                provider=_provider_key(provider),
                api_key=api_key,
                model_name=llm_model,
                base_url=base_url,
                badge="SCAN · GLOSSARY AI",
                title="استخراج قاموس الشخصيات والمصطلحات عبر الذكاء الاصطناعي...",
                accepts_progress_cb=False,
            )
            if signs:
                signs = yield from run_live(
                    translate_detected_signs,
                    signs=signs,
                    entries=entries,
                    series_name=series_name,
                    glossary=glossary,
                    provider=_provider_key(provider),
                    api_key=api_key,
                    model_name=llm_model,
                    base_url=base_url,
                    badge="SCAN · SIGN TRANSLATION",
                    title=f"ترجمة {len(signs)} لافتة يابانية مكتشفة...",
                    accepts_progress_cb=False,
                )

        rows, gallery = _signs_to_table_and_gallery(signs)
        glossary_str = json.dumps(glossary, ensure_ascii=False, indent=2) if glossary else "{}"
        state_obj = {
            "primary_vid": str(primary_vid),
            "source_sub": source_sub,
            "is_raw_whisper": is_raw_whisper,
            "signs": signs,
            "work_dir": str(work_dir),
        }

        push(f"✅ جاهز للمراجعة السريعة! راجع الجدول والمصغرات بالأسفل، شيل الصح من أي لافتة غير مرغوبة ثم اضغط [STRIKE].")
        yield pack(
            render_progress_hud(
                100.0,
                "QUICK REVIEW READY · 100%",
                f"تم لقط {len(signs)} لافتة صامتة وتجهيز القاموس — راجعها بالأسفل ثم اضغط [STRIKE]!",
                f"{len(entries)} سطر حوار جاهز",
                primary_vid.name,
                mode="done",
            ),
            gal=gallery,
            tbl=rows,
            gls=glossary_str,
            raw_dd=gr.update(choices=[""] + list_cached_raw_videos()),
            st=state_obj,
        )
    except Exception as exc:
        push(f"❌ خطأ أثناء الفحص السريع: {exc}\n{traceback.format_exc()[-450:]}")
        yield pack(
            render_progress_hud(0.0, "SCAN ERROR", f"خطأ أثناء الفحص: {str(exc)[:100]}", "راجع السجل", "", mode="error")
        )


def step1_process_for_preview(
    episode_name: str,
    series_name: str,
    style_preset: str,
    saved_raw_1080: str,
    link_1080: str, file_1080: Optional[str],
    link_720: str, file_720: Optional[str],
    link_480: str, file_480: Optional[str],
    sub_mode: str,
    whisper_model: str,
    auto_scan_if_skipped: bool,
    min_sign_h_pct: float,
    skip_op_ed_signs: bool,
    signs_table: Any,
    glossary_box: str,
    scan_state: Dict,
    provider: str,
    api_key: str,
    llm_model: str,
    base_url: str,
    codec: str,
    cq: int,
    preset: str,
    audio_bitrate: str,
    credit_translator: str,
    credit_typesetter: str,
    credit_encoder: str,
    cleanup_temp: bool,
):
    logs: List[str] = []
    rendered_map: Dict[str, str] = {}
    ass_out: Optional[str] = None

    def push(msg: str) -> None:
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def pack(
        hud_html: str,
        preview_val=gr.update(),
        local_dd=gr.update(),
        raw_dd=gr.update(),
        gal=gr.update(),
        tbl=gr.update(),
        gls=gr.update(),
        vids_val=gr.update(),
        ass_val=gr.update(),
        state_val=gr.update(),
    ):
        return (hud_html, "\n".join(logs[-150:]), preview_val, local_dd, raw_dd, gal, tbl, gls, vids_val, ass_val, state_val)

    def run_live(fn: Callable, *args, badge: str, title: str, mode: str = "cyan", accepts_progress_cb: bool = True, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        if accepts_progress_cb:
            kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except Exception as e:
                res_box["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        cur_pct, cur_met, cur_det = 0.0, "جاري البدء...", ""
        dirty = True
        last_yield = 0.0

        while t.is_alive() or not ev_q.empty():
            try:
                items = [ev_q.get(timeout=0.25)]
            except queue.Empty:
                items = []
            while not ev_q.empty():
                try:
                    items.append(ev_q.get_nowait())
                except queue.Empty:
                    break
            for k, p in items:
                dirty = True
                if k == "prog":
                    cur_pct, cur_met, cur_det = p  # type: ignore[misc]
                else:
                    msg_s = str(p)
                    if msg_s.startswith("[whisper_prog]"):
                        parts = msg_s.replace("[whisper_prog]", "").strip().split("|")
                        try:
                            cur_pct = float(parts[0].replace("%", "").strip())
                        except Exception:
                            pass
                        cur_met = "تفريغ صوتي بـ Faster-Whisper ⚡"
                        cur_det = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        push(msg_s)
                        bm = re.search(r"Translated batch\s+(\d+)/(\d+)", msg_s)
                        if bm:
                            bx, by = int(bm.group(1)), int(bm.group(2))
                            cur_pct = (bx / max(1, by)) * 100.0
                            cur_met = f"تمت ترجمة الدفعة {bx} من {by}"
                            cur_det = llm_model

            now_m = time.monotonic()
            if dirty and (now_m - last_yield >= 0.35 or not t.is_alive()):
                last_yield = now_m
                dirty = False
                yield pack(render_progress_hud(cur_pct, badge, title, cur_met, cur_det, mode=mode))

        if "err" in res_box:
            raise res_box["err"]  # type: ignore[misc]
        return res_box.get("val")

    eff_1080 = saved_raw_1080 if (saved_raw_1080 and os.path.isfile(saved_raw_1080)) else file_1080
    quality_inputs = [
        (lbl, lnk, fl)
        for lbl, lnk, fl in [("1080p", link_1080, eff_1080), ("720p", link_720, file_720), ("480p", link_480, file_480)]
        if (lnk and lnk.strip()) or fl
    ]
    if not quality_inputs:
        push("⚠️ أدخل رابط تورنت أو اختر حلقة خام محفوظة من القائمة قبل الانطلاق.")
        yield pack(render_progress_hud(0.0, "تنبيه · لا يوجد مصدر", "أدخل رابط أو اختر حلقة محفوظة", "—", "—", mode="error"), state_val={})
        return

    auto_base_name = episode_name.strip() or (Path(eff_1080).stem if (eff_1080 and os.path.isfile(eff_1080)) else "")
    ep_folder_name = _safe_name(auto_base_name)
    ep_out_dir = STORAGE_ROOT / ep_folder_name
    work_dir = RAW_CACHE_ROOT / "_work" / ep_folder_name
    ep_out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        downloaded: Dict[str, Path] = {}
        used_cache_dirs: List[Path] = []
        for q_label, lnk, fl in quality_inputs:
            cdir = _raw_cache_dir_for_source(q_label, lnk, fl, ep_folder_name)
            used_cache_dirs.append(cdir)
            vid_path = yield from run_live(
                download_torrent_or_url,
                lnk, fl, str(cdir),
                badge=f"STAGE 1/4 · DOWNLOAD [{q_label}]",
                title=f"تحميل / فحص الحلقة الخام بجودة {q_label}",
                accepts_progress_cb=True,
            )
            downloaded[q_label] = vid_path

        updated_raw_list = [""] + list_cached_raw_videos()
        primary_vid = downloaded[quality_inputs[0][0]]

        # Reuse scan_state subtitle if matching same primary video
        source_sub = None
        is_raw_whisper = False
        if scan_state and scan_state.get("primary_vid") == str(primary_vid) and os.path.isfile(str(scan_state.get("source_sub", ""))):
            source_sub = str(scan_state["source_sub"])
            is_raw_whisper = bool(scan_state.get("is_raw_whisper", False))
            push("⚡ استخدام ملف التفريغ/الترجمة المستخرج مسبقاً من خطوة المراجعة السريعة.")
        else:
            if sub_mode.startswith("Auto"):
                probe = probe_embedded_subtitles(str(primary_vid))
                if probe is not None:
                    source_sub = extract_embedded_subtitle(str(primary_vid), probe[0], probe[1], str(work_dir))
            if source_sub is None:
                is_raw_whisper = True
                source_sub = yield from run_live(
                    transcribe_with_whisper,
                    str(primary_vid),
                    str(work_dir / "whisper.srt"),
                    model_size=whisper_model,
                    badge=f"STAGE 2/4 · WHISPER [{whisper_model}]",
                    title="تفريغ الصوت الياباني بـ Faster-Whisper مع طوابع الكلمات...",
                    accepts_progress_cb=False,
                )

        entries = parse_source_subtitles(source_sub)

        # Parse edited glossary from UI or auto-extract
        glossary: Dict = {}
        if glossary_box and glossary_box.strip() and glossary_box.strip() != "{}":
            try:
                glossary = json.loads(glossary_box)
                if series_name.strip():
                    save_series_glossary(series_name, glossary)
            except Exception:
                glossary = {}
        if not glossary:
            glossary = yield from run_live(
                load_or_extract_series_glossary,
                series_name=series_name,
                entries=entries,
                is_raw_whisper=is_raw_whisper,
                provider=_provider_key(provider),
                api_key=api_key,
                model_name=llm_model,
                base_url=base_url,
                badge="STAGE 2/4 · SERIES GLOSSARY",
                title="تجهيز قاموس أسماء ومصطلحات الأنمي...",
                accepts_progress_cb=False,
            )

        # Merge Quick Review table edits (or auto-scan if user skipped [SCAN] and enabled auto_scan)
        approved_signs: List[Dict] = []
        ui_gal = gr.update()
        ui_tbl = gr.update()
        if scan_state and scan_state.get("primary_vid") == str(primary_vid) and scan_state.get("signs"):
            approved_signs = _merge_table_edits_into_signs(scan_state["signs"], signs_table)
        elif auto_scan_if_skipped:
            scanned = yield from run_live(
                scan_japanese_signs,
                str(primary_vid),
                str(work_dir),
                min_height_pct=max(3.2, float(min_sign_h_pct)),
                skip_op_ed_credits=bool(skip_op_ed_signs),
                badge="STAGE 2/4 · AUTO SIGN OCR",
                title="فحص تلقائي للافتات اليابانية الواضحة...",
                accepts_progress_cb=True,
            )
            if scanned:
                approved_signs = yield from run_live(
                    translate_detected_signs,
                    signs=scanned,
                    entries=entries,
                    series_name=series_name,
                    glossary=glossary,
                    provider=_provider_key(provider),
                    api_key=api_key,
                    model_name=llm_model,
                    base_url=base_url,
                    badge="STAGE 2/4 · SIGN TRANSLATION",
                    title=f"ترجمة {len(scanned)} لافتة مكتشفة...",
                    accepts_progress_cb=False,
                )
                ui_tbl, ui_gal = _signs_to_table_and_gallery(approved_signs)

        cp_file = str(work_dir / "translation_checkpoint.json")
        translations = yield from run_live(
            translate_entries,
            entries=entries,
            provider=_provider_key(provider),
            api_key=api_key,
            model_name=llm_model,
            base_url=base_url,
            series_name=series_name,
            glossary=glossary,
            checkpoint_path=cp_file,
            badge="STAGE 3/4 · CELESTIAL TRANSLATION",
            title=f"ترجمة {len(entries)} سطر بأسلوب Celestial-Dragons عبر {llm_model}...",
            accepts_progress_cb=False,
        )

        credits_dict = {
            "translator": credit_translator,
            "typesetter": credit_typesetter,
            "encoder": credit_encoder,
        }
        has_credits = any(v.strip() for v in credits_dict.values())
        active_sign_count = sum(1 for s in approved_signs if s.get("enabled", True) and str(s.get("ar_text", "")).strip())

        for q_label, vid_path in downloaded.items():
            vw, vh, _, _ = probe_video_info(str(vid_path))
            q_ass = str(ep_out_dir / f"{ep_folder_name}_{q_label}_AR.ass")
            write_responsive_ass(
                entries=entries,
                translations=translations,
                out_ass_path=q_ass,
                target_w=vw,
                target_h=vh,
                style_preset=style_preset,
                approved_visual_signs=approved_signs,
                credits=credits_dict if has_credits else None,
            )
            if ass_out is None:
                ass_out = q_ass
            push(f"📐 تم توليد ملف ASS نسبي لجودة {q_label} ({vw}x{vh} · لافتات ممسوحة={active_sign_count})")

            patches_for_q: List[Dict] = []
            if active_sign_count > 0:
                patches_for_q = yield from run_live(
                    build_inpainted_sign_patches,
                    str(vid_path),
                    [s for s in approved_signs if s.get("enabled", True) and str(s.get("ar_text", "")).strip()],
                    str(work_dir / f"patches_{q_label}"),
                    badge=f"STAGE 4/4 · LAMA INPAINT [{q_label}]",
                    title=f"محو الكتابة اليابانية من الفيديو بـ LaMa GPU لجودة {q_label} ({vw}x{vh})...",
                    accepts_progress_cb=True,
                )

            out_mp4 = str(ep_out_dir / f"{ep_folder_name}_{q_label}_AR.mp4")
            yield from run_live(
                burn_and_compress_native,
                video_path=str(vid_path),
                ass_path=q_ass,
                output_path=out_mp4,
                fonts_dir=str(FONTS_DIR),
                codec=codec,
                cq=int(cq),
                preset=preset,
                audio_bitrate=audio_bitrate,
                sign_patches=patches_for_q,
                badge=f"STAGE 4/4 · NVENC ENCODE [{q_label}]",
                title=f"دمج اللافتات الممسوحة وحرق الترجمة لجودة {q_label} ({codec})...",
                accepts_progress_cb=True,
            )
            rendered_map[q_label] = out_mp4

        shutil.rmtree(work_dir, ignore_errors=True)
        if cleanup_temp:
            for cdir in used_cache_dirs:
                if cdir.exists():
                    shutil.rmtree(cdir, ignore_errors=True)
            updated_raw_list = [""] + list_cached_raw_videos()

        first_video = next(iter(rendered_map.values()))
        all_local = list_local_outputs()

        push(f"✅ اكتمل الإنتاج بالكامل! عاين الفيديو الآن، ثم اضغط [S-PORTAL] للرفع السحابي.")
        yield pack(
            render_progress_hud(
                100.0,
                "MISSION COMPLETE · 100%",
                "اكتملت الترجمة ومحو اللافتات والحرق النسبي بنجاح! الفيديو جاهز للمعاينة",
                f"تم إنتاج {len(rendered_map)} جودة · محو {active_sign_count} لافتة",
                str(ep_out_dir),
                mode="done",
            ),
            preview_val=first_video,
            local_dd=gr.update(choices=all_local, value=first_video),
            raw_dd=gr.update(choices=updated_raw_list),
            gal=ui_gal,
            tbl=ui_tbl,
            gls=json.dumps(glossary, ensure_ascii=False, indent=2),
            vids_val=list(rendered_map.values()),
            ass_val=ass_out,
            state_val={"folder": str(ep_out_dir), "files": rendered_map},
        )
    except Exception as exc:
        push(f"❌ حدث خطأ أثناء التجهيز: {exc}\n{traceback.format_exc()[-500:]}")
        yield pack(
            render_progress_hud(
                0.0,
                "ERROR · SYSTEM HALTED",
                f"توقف بسبب خطأ: {str(exc)[:110]}",
                "نقطة حفظ الترجمة والملف الخام ما زالا محفوظين",
                "راجع سجل العمليات",
                mode="error",
            ),
            raw_dd=gr.update(choices=[""] + list_cached_raw_videos()),
            vids_val=list(rendered_map.values()) or None,
            ass_val=ass_out,
            state_val={},
        )


def step2_approve_and_upload(
    job_state: Dict,
    enable_mega: bool, mega_remote: str, mega_folder: str,
    enable_dood: bool, dood_api_key: str,
    enable_gdrive: bool, gdrive_remote: str, gdrive_folder: str,
):
    if not job_state or not job_state.get("files"):
        yield (
            render_progress_hud(0.0, "S-PORTAL · NO FILES", "شغّل المرحلة [STRIKE] للمعاينة أولاً قبل الرفع.", "—", "—", mode="error"),
            "⚠️ لا توجد فيديوهات جاهزة للرفع حالياً.",
            "",
        )
        return

    logs: List[str] = []
    links_report: List[str] = [f"📁 مسار الحفظ المحلي على RunPod:\n  {job_state['folder']}\n"]

    def push(msg: str) -> None:
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def run_upload_live(fn: Callable, *args, badge: str, title: str, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except Exception as e:
                res_box["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        cur_pct, cur_met, cur_det = 1.0, "جاري الاتصال بالسحابة...", ""
        dirty = True
        last_y = 0.0
        while t.is_alive() or not ev_q.empty():
            try:
                items = [ev_q.get(timeout=0.25)]
            except queue.Empty:
                items = []
            while not ev_q.empty():
                try:
                    items.append(ev_q.get_nowait())
                except queue.Empty:
                    break
            for k, p in items:
                dirty = True
                if k == "prog":
                    cur_pct, cur_met, cur_det = p  # type: ignore[misc]
                else:
                    push(str(p))
            now_m = time.monotonic()
            if dirty and (now_m - last_y >= 0.35 or not t.is_alive()):
                last_y = now_m
                dirty = False
                yield (
                    render_progress_hud(cur_pct, badge, title, cur_met, cur_det, mode="violet"),
                    "\n".join(logs[-150:]),
                    "\n".join(links_report),
                )
        if "err" in res_box:
            raise res_box["err"]  # type: ignore[misc]
        return res_box.get("val")

    try:
        for q_label, mp4_path in job_state["files"].items():
            links_report.append(f"=== جودة {q_label} ({Path(mp4_path).name}) ===")
            links_report.append(f"  Local Path: {mp4_path}")

            if enable_mega:
                mega_link = yield from run_upload_live(
                    upload_to_rclone, mp4_path, mega_remote, mega_folder, generate_link=True,
                    badge=f"S-PORTAL · MEGA [{q_label}]", title=f"جاري رفع نسخة {q_label} على MEGA...",
                )
                links_report.append(f"  MEGA: {mega_link}")

            if enable_dood:
                dood_res = yield from run_upload_live(
                    upload_to_doodstream, mp4_path, dood_api_key,
                    badge=f"S-PORTAL · DOODSTREAM [{q_label}]", title=f"جاري رفع نسخة {q_label} على DoodStream...",
                )
                links_report.append(f"  DoodStream Watch: {dood_res['watch_url']}")
                links_report.append(f"  DoodStream Embed: {dood_res['embed_url']}")

            if enable_gdrive:
                gd_res = yield from run_upload_live(
                    upload_to_rclone, mp4_path, gdrive_remote, gdrive_folder, generate_link=False,
                    badge=f"S-PORTAL · GOOGLE DRIVE [{q_label}]", title=f"جاري رفع نسخة {q_label} على Google Drive...",
                )
                links_report.append(f"  Google Drive: {gd_res}")
            links_report.append("")

        (Path(job_state["folder"]) / "links.txt").write_text("\n".join(links_report), encoding="utf-8")
        yield (
            render_progress_hud(100.0, "S-PORTAL · UPLOAD COMPLETE", "اكتمل رفع جميع الجودات واستخراج الروابط!", "100% Cloud Sync", job_state["folder"], mode="done"),
            "\n".join(logs[-150:]),
            "\n".join(links_report),
        )
    except Exception as exc:
        push(f"❌ خطأ أثناء الرفع: {exc}")
        yield (
            render_progress_hud(0.0, "S-PORTAL · ERROR", f"خطأ أثناء الرفع: {str(exc)[:100]}", "راجع السجل", "", mode="error"),
            "\n".join(logs[-150:]),
            "\n".join(links_report),
        )


def _build_blocks_kwargs() -> Dict:
    sig = inspect.signature(gr.Blocks.__init__).parameters
    kwargs: Dict = {"title": "نصل الظل · سَبّر الأنمي | Celestial DeepSeek Edition"}
    if "theme" in sig:
        kwargs["theme"] = gr.themes.Base(primary_hue="sky", secondary_hue="violet", neutral_hue="slate")
    if "css" in sig:
        kwargs["css"] = SHADOW_BLADE_CSS
    if "head" in sig:
        kwargs["head"] = SHADOW_BLADE_HEAD
    return kwargs


def build_ui() -> gr.Blocks:
    cfg = load_settings()

    with gr.Blocks(**_build_blocks_kwargs()) as demo:
        gr.HTML(f"<style>{SHADOW_BLADE_CSS}</style>{SHADOW_BLADE_HEAD}")
        gr.HTML(SHADOW_BLADE_HERO_HTML)

        job_state = gr.State({})
        scan_state = gr.State({})

        with gr.Row():
            with gr.Column(scale=2):
                with gr.Row():
                    episode_name = gr.Textbox(
                        label="📌 اسم الحلقة / مجلد الإخراج",
                        placeholder="مثال: Bleach_EP366 (أو اتركه فارغاً ليأخذ اسم الملف)",
                        scale=2,
                    )
                    series_name = gr.Textbox(
                        label="🎌 اسم الأنمي (لتوحيد وحفظ قاموس الأسماء والمصطلحات تلقائياً)",
                        placeholder="مثال: Bleach أو Black Clover أو Jujutsu Kaisen",
                        scale=2,
                    )
                    style_preset = gr.Dropdown(
                        choices=[
                            ("Black Clover Style (ScaleX 90% · MarginV 4.35%H · Top Style)", "black_clover_style"),
                            ("Bleach Style (ScaleX 95% · MarginV 2.78%H · Inline \\an8)", "bleach_style"),
                        ],
                        value=cfg.get("style_preset", "black_clover_style"),
                        label="📐 بروفايل الأبعاد النسبية (%W/%H)",
                        scale=2,
                    )

                with gr.Accordion("🔗 الحلقات المحفوظة على السيرفر أو روابط التورنت (حفظ دائم بدون إعادة تحميل)", open=True, elem_classes=["sb-accordion"]):
                    with gr.Row():
                        saved_raw_1080 = gr.Dropdown(
                            choices=[""] + list_cached_raw_videos(),
                            value="",
                            label="📦 اختر حلقة خام محمّلة مسبقاً على السيرفر (/workspace/raw_episodes)",
                            interactive=True,
                            scale=4,
                        )
                        refresh_raw_btn = gr.Button("🔄 تحديث المحفوظ", size="sm", scale=1)

                    with gr.Row():
                        link_1080 = gr.Textbox(label="رابط 1080p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_1080 = gr.File(label="أو ملف .torrent / فيديو 1080p", type="filepath")
                    with gr.Row():
                        link_720 = gr.Textbox(label="رابط 720p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_720 = gr.File(label="أو ملف .torrent / فيديو 720p", type="filepath")
                    with gr.Row():
                        link_480 = gr.Textbox(label="رابط 480p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_480 = gr.File(label="أو ملف .torrent / فيديو 480p", type="filepath")

                with gr.Accordion("👁️ المراجعة السريعة للافتات اليابانية الصامتة + قاموس الأنمي (LaMa Inpainting)", open=True, elem_classes=["sb-accordion"]):
                    with gr.Row():
                        min_sign_h_pct = gr.Slider(
                            minimum=1.8, maximum=8.0, step=0.2, value=2.6,
                            label="أقل حجم للافتة (% من ارتفاع الشاشة لتجاهل التفاصيل الهامشية)",
                        )
                        skip_op_ed_signs = gr.Checkbox(
                            value=True,
                            label="تجاهل كتل حقوق أغنية البداية والنهاية الكثيفة تلقائياً",
                        )
                        auto_scan_if_skipped = gr.Checkbox(
                            value=False,
                            label="فحص ومحو تلقائي لو ضغطت [STRIKE] مباشرة بدون فحص مسبق",
                        )

                    scan_btn = gr.Button(
                        "🔍 الخطوة 1: فحص سريع للافتات الصامتة واستخراج القاموس للمراجعة [SCAN]",
                        elem_id="sb-scan-btn",
                    )

                    signs_gallery = gr.Gallery(
                        label="🖼️ مصغرات اللافتات اليابانية المكتشفة على الشاشة (راجع الرقم # مع الجدول)",
                        columns=4,
                        height=210,
                        object_fit="contain",
                    )

                    signs_table = gr.Dataframe(
                        headers=["محو وترجمة ✓", "#", "البداية", "النهاية", "النص الياباني", "الترجمة العربية (عدّلها بحرية)", "لون الخط", "لون الحواف"],
                        datatype=["bool", "number", "str", "str", "str", "str", "str", "str"],
                        col_count=(8, "fixed"),
                        type="array",
                        interactive=True,
                        label="📋 جدول المراجعة السريعة (ارفع علامة ✓ من أي لافتة لا تريد محوها قبل الضغط على STRIKE)",
                    )

                    with gr.Accordion("📚 قاموس الأنمي المستخرج (يُحفظ تلقائياً لكل حلقات الأنمي القادمة — قابل للتعديل)", open=False):
                        glossary_box = gr.Code(
                            value="{}",
                            language="json",
                            label="Series Glossary JSON (/workspace/glossaries)",
                            lines=8,
                        )

                with gr.Accordion("🔑 مفاتيح DeepSeek / Gemini والإعدادات الدائمة", open=True, elem_classes=["sb-accordion"]):
                    with gr.Row():
                        provider = gr.Dropdown(
                            choices=["DeepSeek API (موصى به)", "Gemini API", "OpenAI-Compatible / Local"],
                            value=cfg["provider"],
                            label="محرك الترجمة الذكي (مع تفعيل البديل التلقائي deepseek-v4-pro)",
                        )
                        llm_model = gr.Textbox(value=cfg["llm_model"], label="اسم الموديل الأساسي")
                    with gr.Row():
                        api_key = gr.Textbox(value=cfg["api_key"], label="DeepSeek / Gemini API Key", type="password")
                        base_url = gr.Textbox(value=cfg["base_url"], label="Base URL")
                    with gr.Row():
                        dood_api_key = gr.Textbox(value=cfg["dood_api_key"], label="DoodStream API Key", type="password")
                        mega_remote = gr.Textbox(value=cfg["mega_remote"], label="MEGA Remote")
                        mega_folder = gr.Textbox(value=cfg["mega_folder"], label="فولدر MEGA")
                    with gr.Row():
                        gdrive_remote = gr.Textbox(value=cfg["gdrive_remote"], label="Google Drive Remote")
                        gdrive_folder = gr.Textbox(value=cfg["gdrive_folder"], label="فولدر Drive")

                    save_btn = gr.Button("💾 حفظ جميع المفاتيح والإعدادات في النظام", variant="secondary")
                    save_status = gr.Markdown("")

                with gr.Accordion("⚙️ إعدادات Whisper والضغط (NVENC) وحقوق الفريق", open=False, elem_classes=["sb-accordion"]):
                    with gr.Row():
                        sub_mode = gr.Radio(
                            choices=[
                                "Auto (سحب ترجمة MKV إن وجدت، وإلا Whisper)",
                                "Force Whisper (تفريغ صوتي دائماً)",
                            ],
                            value="Auto (سحب ترجمة MKV إن وجدت، وإلا Whisper)",
                            label="مصدر الترجمة",
                        )
                        whisper_model = gr.Dropdown(choices=["large-v3", "medium", "small"], value="large-v3", label="موديل Whisper")
                    with gr.Row():
                        codec = gr.Dropdown(choices=["h264_nvenc", "hevc_nvenc", "av1_nvenc"], value=cfg["codec"], label="الكوديك (GPU NVENC)")
                        preset = gr.Dropdown(choices=["p7", "p6", "p5", "p4"], value=cfg["preset"], label="دقة الضغط")
                        cq = gr.Slider(minimum=18, maximum=30, step=1, value=cfg["cq"], label="الجودة CQ")
                        audio_bitrate = gr.Dropdown(choices=["128k", "160k", "192k"], value=cfg["audio_bitrate"], label="صوت AAC")
                    with gr.Row():
                        credit_translator = gr.Textbox(value=cfg["credit_translator"], label="الترجمة")
                        credit_typesetter = gr.Textbox(value=cfg["credit_typesetter"], label="المحاكاة")
                        credit_encoder = gr.Textbox(value=cfg["credit_encoder"], label="الدمج والرفع")

                    cleanup_temp = gr.Checkbox(value=False, label="مسح الحلقة الخام بعد انتهاء الدمج (اتركه مغلقاً للاحتفاظ بالحلقات المحمّلة دائماً)")

                progress_hud = gr.HTML(value=render_progress_hud())

                step1_btn = gr.Button(
                    "⚔️ الخطوة 2: انطلق — محو اللافتات المختارة والترجمة والحرق النسبي للمعاينة! [STRIKE]",
                    variant="primary",
                    size="lg",
                    elem_id="sb-step1-btn",
                )

                with gr.Row():
                    enable_mega = gr.Checkbox(value=True, label="رفع على MEGA")
                    enable_dood = gr.Checkbox(value=True, label="رفع على DoodStream")
                    enable_gdrive = gr.Checkbox(value=False, label="رفع على Google Drive")

                step2_btn = gr.Button(
                    "🌀 الخطوة 3: عبور البوابة — اعتماد ورفع جميع الجودات [S-PORTAL]",
                    variant="stop",
                    size="lg",
                    elem_id="sb-step2-btn",
                )

            with gr.Column(scale=1):
                preview_player = gr.Video(label="📺 مشغّل معاينة الحلقة بعد المحو والحرق")

                with gr.Row():
                    local_dropdown = gr.Dropdown(choices=list_local_outputs(), label="📂 تصفّح الحلقات المحروقة الجاهزة (/workspace/outputs)", interactive=True)
                    refresh_local_btn = gr.Button("🔄 تحديث", size="sm")

                links_box = gr.Textbox(label="🔗 روابط الرفع الجاهزة للنشر", lines=8, interactive=False)
                log_box = gr.Textbox(label="📋 سجل عمليات النظام المباشر [HUNTER LOG]", lines=16, max_lines=22, interactive=False)
                out_video_files = gr.Files(label="🎥 تحميل الفيديوهات النهائية لجهازك")
                out_ass_file = gr.File(label="📝 تحميل ملف الترجمة المنسّق (.ass)")

        provider.change(fn=_on_provider_change, inputs=[provider], outputs=[llm_model, base_url])

        save_btn.click(
            fn=save_settings,
            inputs=[
                provider, llm_model, api_key, base_url, style_preset, dood_api_key,
                mega_remote, mega_folder, gdrive_remote, gdrive_folder,
                codec, preset, cq, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
            ],
            outputs=[save_status],
        )

        refresh_raw_btn.click(fn=lambda: gr.update(choices=[""] + list_cached_raw_videos()), outputs=[saved_raw_1080])
        refresh_local_btn.click(fn=lambda: gr.update(choices=list_local_outputs()), outputs=[local_dropdown])
        local_dropdown.change(fn=lambda p: p, inputs=[local_dropdown], outputs=[preview_player])

        scan_btn.click(
            fn=step0_quick_scan_signs_and_glossary,
            inputs=[
                episode_name, series_name,
                saved_raw_1080,
                link_1080, file_1080,
                link_720, file_720,
                link_480, file_480,
                sub_mode, whisper_model,
                min_sign_h_pct, skip_op_ed_signs,
                provider, api_key, llm_model, base_url,
            ],
            outputs=[progress_hud, log_box, signs_gallery, signs_table, glossary_box, saved_raw_1080, scan_state],
        )

        step1_btn.click(
            fn=step1_process_for_preview,
            inputs=[
                episode_name, series_name, style_preset,
                saved_raw_1080,
                link_1080, file_1080,
                link_720, file_720,
                link_480, file_480,
                sub_mode, whisper_model,
                auto_scan_if_skipped, min_sign_h_pct, skip_op_ed_signs,
                signs_table, glossary_box, scan_state,
                provider, api_key, llm_model, base_url,
                codec, cq, preset, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
                cleanup_temp,
            ],
            outputs=[
                progress_hud, log_box, preview_player, local_dropdown, saved_raw_1080,
                signs_gallery, signs_table, glossary_box, out_video_files, out_ass_file, job_state,
            ],
        )

        step2_btn.click(
            fn=step2_approve_and_upload,
            inputs=[
                job_state,
                enable_mega, mega_remote, mega_folder,
                enable_dood, dood_api_key,
                enable_gdrive, gdrive_remote, gdrive_folder,
            ],
            outputs=[progress_hud, log_box, links_box],
        )

    return demo


if __name__ == "__main__":
    ui = build_ui()
    user, pwd = os.environ.get("UI_USER", "").strip(), os.environ.get("UI_PASS", "").strip()

    launch_sig = inspect.signature(ui.launch).parameters
    launch_kwargs = {
        "server_name": "0.0.0.0",
        "server_port": int(os.environ.get("PORT", "7860")),
        "share": os.environ.get("GRADIO_SHARE", "false").lower() == "true",
        "auth": (user, pwd) if (user and pwd) else None,
    }
    if "css" in launch_sig:
        launch_kwargs["css"] = SHADOW_BLADE_CSS
    if "head" in launch_sig:
        launch_kwargs["head"] = SHADOW_BLADE_HEAD

    ui.queue().launch(**launch_kwargs)
