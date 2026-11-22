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
    burn_and_compress_native,
    qc_burned_episode,
    collect_episode_videos,
    download_season_batch,
    download_torrent_or_url,
    episode_sort_key,
    extract_episode_number,
    make_browser_preview,
    probe_video_info,
    rclone_remote_configured,
    drive_download_file,
    upload_to_doodstream,
    upload_to_rclone,
)
from subtitles_ai import (
    _glossary_dir,
    _glossary_slug,
    ensure_style_profile,
    extract_embedded_subtitle,
    learn_glossary_from_translation,
    load_or_extract_series_glossary,
    parse_source_subtitles,
    probe_embedded_subtitles,
    save_series_glossary,
    transcribe_with_whisper,
    translate_entries,
    write_responsive_ass,
)

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"
LOGO_FILE = BASE_DIR / "assets" / "logo.png"
STORAGE_ROOT = Path(os.environ.get("RUNPOD_OUTPUT_ROOT", "/workspace/outputs"))
RAW_CACHE_ROOT = Path(os.environ.get("RUNPOD_RAW_CACHE", "/workspace/raw_episodes"))
LEGACY_TEMP_ROOT = Path(os.environ.get("RUNPOD_TEMP_ROOT", "/workspace/tmp_downloads"))
SETTINGS_FILE = Path(os.environ.get("RUNPOD_SETTINGS_FILE", "/workspace/subber_settings.json"))

# مفاتيح محفوظة في ملف subber_secrets.json جنب الكود (بتتقري تلقائياً عند التشغيل)
_SECRETS_FILE = BASE_DIR / "subber_secrets.json"
if _SECRETS_FILE.is_file():
    try:
        for _k, _v in json.loads(_SECRETS_FILE.read_text(encoding="utf-8")).items():
            if isinstance(_v, str) and _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass

ensure_style_profile()


def _seed_rclone_from_secrets() -> None:
    """Fresh pod: rebuild ~/.config/rclone/rclone.conf for the gdrive remote from the
    saved Drive token in subber_secrets.json, so backups work without notebook cell 2.
    rclone refreshes the access token by itself via the refresh_token."""
    try:
        if shutil.which("rclone") is None:
            return
        conf = Path.home() / ".config" / "rclone" / "rclone.conf"
        tok = os.environ.get("GDRIVE_TOKEN", "").strip()
        if tok and not conf.is_file():
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text(
                f"[gdrive]\ntype = drive\nscope = drive\ntoken = {tok}\n",
                encoding="utf-8",
            )
    except Exception:
        pass


_seed_rclone_from_secrets()


def _drive_restore_glossaries() -> None:
    """Fresh pod: pull back saved per-series glossaries from Drive (best effort)."""
    try:
        _st = load_settings()
        _drv_remote = str(_st.get("gdrive_remote", "gdrive")).strip() or "gdrive"
        if not rclone_remote_configured(_drv_remote):
            return
        _drv_folder = str(_st.get("gdrive_folder", "Runpod_Anime")).strip() or "Runpod_Anime"
        gdir = _glossary_dir()
        gdir.mkdir(parents=True, exist_ok=True)
        subprocess_res = os.system(
            f"rclone copy {_drv_remote}:{_drv_folder}/_state/glossaries "
            + str(gdir) + " --ignore-existing >/dev/null 2>&1"
        )
        _ = subprocess_res
    except Exception:
        pass


def _drive_backup_glossary(series_name: str) -> bool:
    """Persist the per-series glossary to Drive so character names survive a pod wipe.
    Best effort: never raises, never blocks the run."""
    try:
        _st = load_settings()
        drv_remote = str(_st.get("gdrive_remote", "gdrive")).strip() or "gdrive"
        if not rclone_remote_configured(drv_remote):
            return False
        drv_folder = str(_st.get("gdrive_folder", "Runpod_Anime")).strip() or "Runpod_Anime"
        gfile = _glossary_dir() / f"{_glossary_slug(series_name)}.json"
        if not gfile.is_file():
            return False
        upload_to_rclone(str(gfile), drv_remote, f"{drv_folder}/_state/glossaries", generate_link=False)
        return True
    except Exception:
        return False


SERIES_NEW_OPT = "➕ أنمي جديد — اكتب الاسم بنفسك"


def list_saved_series() -> List[Tuple[str, str]]:
    """(label, value) choices for every saved per-series glossary, newest first.
    Runs after the Drive restore at startup, so a fresh pod still lists his anime."""
    out: List[Tuple[str, str]] = []
    try:
        gdir = _glossary_dir()
        if gdir.is_dir():
            files = sorted(gdir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            for fp in files:
                name = ""
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    name = str(data.get("series", "")).strip()
                except Exception:
                    pass
                if not name:
                    name = fp.stem.replace("_", " ").strip().title()
                if name:
                    out.append((f"📚 {name}", name))
    except Exception:
        pass
    return out


def _default_series_pick() -> str:
    saved = list_saved_series()
    return saved[0][1] if saved else SERIES_NEW_OPT

SHADOW_BLADE_HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@500;600;700;800;900&family=Changa:wght@600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">

<script>
(function () {
  function boot() {
    if (!document.body) { setTimeout(boot, 80); return; }
    if (document.getElementById('sb-global-vfx-canvas')) return;
    var cv = document.createElement('canvas'); cv.id = 'sb-global-vfx-canvas'; document.body.appendChild(cv);
    var vig = document.createElement('div'); vig.className = 'sb-vig'; document.body.appendChild(vig);
    var flash = document.createElement('div'); flash.id = 'sb-flash'; document.body.appendChild(flash);
    var arise = document.createElement('div'); arise.id = 'sb-arise'; arise.textContent = 'ARISE'; document.body.appendChild(arise);
    var alertW = document.createElement('div'); alertW.id = 'sb-sysalert';
    alertW.innerHTML = '<div class="win"><div class="cap">\u26A0 SYSTEM ALERT</div><div class="big">RANK UP!</div><div class="sub" id="sb-sysalert-sub"></div></div>';
    document.body.appendChild(alertW);

    var cx = cv.getContext('2d'), W = 0, H = 0;
    function resize() {
      var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      W = window.innerWidth; H = window.innerHeight;
      cv.width = W * dpr; cv.height = H * dpr;
      cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    window.addEventListener('resize', resize); resize();

    function newWisp() {
      return { x: Math.random() * W, y: H + 30 + Math.random() * 90, r: 10 + Math.random() * 26,
        vy: 12 + Math.random() * 24, ph: Math.random() * 6.28, sw: 12 + Math.random() * 26,
        hue: 258 + Math.random() * 28, a: .10 + Math.random() * .15 };
    }
    var wisps = [], motes = [], sparks = [], rings = [], i;
    for (i = 0; i < 26; i++) { var w = newWisp(); w.y = Math.random() * H; wisps.push(w); }
    for (i = 0; i < 70; i++) motes.push({ x: Math.random() * W, y: Math.random() * H, r: .6 + Math.random() * 1.7, vy: 5 + Math.random() * 15, ph: Math.random() * 6.28, tw: .5 + Math.random() * 1.6 });
    var combat = 0, combatTarget = 0, last = performance.now(), ema = 60, fpsAt = 0, sweepX = -320;

    function shadowBurst(x, y) {
      for (var j = 0; j < 90; j++) {
        var ang = Math.random() * 6.283, sp = 140 + Math.random() * 520;
        sparks.push({ x: x, y: y, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp - 60,
          r: 1 + Math.random() * 2.6, life: .6 + Math.random() * .9, hue: 258 + Math.random() * 40 });
      }
      rings.push({ x: x, y: y, r: 24, vr: 780, a: .9, decay: 1.1, w: 4, hue: 275 });
      rings.push({ x: x, y: y, r: 12, vr: 560, a: .8, decay: 1.0, w: 2.5, hue: 200 });
    }

    function frame(now) {
      var dt = Math.min(.05, (now - last) / 1000); last = now;
      if (dt > 0) ema = ema * .95 + (1 / dt) * .05;
      if (now - fpsAt > 250) {
        fpsAt = now;
        var f = document.getElementById('sb-fps-meter');
        if (f) f.textContent = Math.round(ema) + ' FPS \u00B7 CANVAS';
      }
      combat += (combatTarget - combat) * Math.min(1, dt * 2.2);
      cx.clearRect(0, 0, W, H);
      cx.globalCompositeOperation = 'lighter';
      sweepX += dt * 340; if (sweepX > W + 320) sweepX = -320;
      var sg = cx.createLinearGradient(sweepX - 90, 0, sweepX + 90, 0);
      sg.addColorStop(0, 'rgba(125,211,252,0)'); sg.addColorStop(.5, 'rgba(125,211,252,.05)'); sg.addColorStop(1, 'rgba(125,211,252,0)');
      cx.fillStyle = sg; cx.fillRect(sweepX - 90, 0, 180, H);
      var k, w, m, g2;
      for (k = 0; k < wisps.length; k++) {
        w = wisps[k];
        w.y -= w.vy * (1 + combat * 2.0) * dt; w.ph += dt * 2;
        var x = w.x + Math.sin(w.ph) * w.sw;
        var fl = .7 + .3 * Math.sin(w.ph * 3.7);
        var rr = w.r * (1 + combat * .5);
        g2 = cx.createRadialGradient(x, w.y, 0, x, w.y, rr);
        g2.addColorStop(0, 'hsla(' + (w.hue - combat * 30) + ',85%,62%,' + (w.a * fl * (1 + combat)) + ')');
        g2.addColorStop(1, 'hsla(270,80%,50%,0)');
        cx.fillStyle = g2; cx.beginPath(); cx.arc(x, w.y, rr, 0, 6.29); cx.fill();
        if (w.y < -70) wisps[k] = newWisp();
      }
      for (k = 0; k < motes.length; k++) {
        m = motes[k];
        m.y -= m.vy * (1 + combat * 2.4) * dt; m.ph += dt * m.tw * 3;
        var a2 = .22 + .55 * Math.abs(Math.sin(m.ph));
        cx.fillStyle = 'hsla(' + (196 + combat * 90) + ',95%,70%,' + a2 + ')';
        cx.beginPath(); cx.arc(m.x + Math.sin(m.ph) * 6, m.y, m.r, 0, 6.29); cx.fill();
        if (m.y < -8) { m.y = H + 8; m.x = Math.random() * W; }
      }
      for (k = rings.length - 1; k >= 0; k--) {
        var r = rings[k]; r.r += r.vr * dt; r.a -= dt * r.decay;
        if (r.a <= 0) { rings.splice(k, 1); continue; }
        cx.strokeStyle = 'hsla(' + r.hue + ',92%,66%,' + r.a + ')'; cx.lineWidth = r.w;
        cx.beginPath(); cx.arc(r.x, r.y, r.r, 0, 6.29); cx.stroke();
      }
      for (k = sparks.length - 1; k >= 0; k--) {
        var s = sparks[k]; s.life -= dt;
        if (s.life <= 0) { sparks.splice(k, 1); continue; }
        s.x += s.vx * dt; s.y += s.vy * dt; s.vy += 26 * dt; s.vx *= (1 - dt * 1.4);
        cx.fillStyle = 'hsla(' + s.hue + ',92%,68%,' + Math.min(1, s.life * 1.4) + ')';
        cx.beginPath(); cx.arc(s.x, s.y, s.r, 0, 6.29); cx.fill();
      }
      cx.globalCompositeOperation = 'source-over';
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    function retrigger(el, cls) { el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls); }
    function strike() {
      combatTarget = 1;
      retrigger(flash, 'go'); retrigger(arise, 'go');
      shadowBurst(W / 2, H * .45);
      setTimeout(function () { shadowBurst(W / 2, H * .45); }, 260);
      var gc = document.querySelector('.gradio-container');
      if (gc) { gc.classList.remove('sb-shake'); void gc.offsetWidth; gc.classList.add('sb-shake');
        setTimeout(function () { gc.classList.remove('sb-shake'); }, 700); }
    }
    document.addEventListener('click', function (e) {
      var b = e.target && e.target.closest ? e.target.closest('#sb-step1-btn, #sb-batch-btn') : null;
      if (b) strike();
    }, true);

    var RANKS = ['D', 'C', 'B', 'A', 'S', 'S+'];
    var lastRank = null;
    setInterval(function () {
      var hud = document.querySelector('.sb-prog-card');
      if (hud && (hud.classList.contains('sb-mode-done') || hud.classList.contains('sb-mode-error'))) combatTarget = 0;
      var r = document.getElementById('sb-rank-live');
      if (r) {
        var rk = r.textContent.replace('RANK', '').trim();
        if (lastRank && rk !== lastRank && RANKS.indexOf(rk) > RANKS.indexOf(lastRank)) {
          document.getElementById('sb-sysalert-sub').textContent = lastRank + ' \u2190 ' + rk;
          retrigger(alertW, 'go');
          shadowBurst(W / 2, H * .4);
        }
        lastRank = rk;
      }
    }, 400);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
</script>
"""

SHADOW_BLADE_CSS = """
:root {
    --sb-void: #030309;
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

/* === SOLO SYSTEM VFX === */
.sb-vig { position: fixed; inset: 0; z-index: 9997; pointer-events: none;
    background: radial-gradient(ellipse at 50% 42%, transparent 44%, rgba(3,3,9,.55) 100%); }
.sb-hero-wrap, .sb-prog-card { border-radius: 2px !important; position: relative; }
.sb-hero-wrap::before, .sb-prog-card::before { content: ''; position: absolute; top: -2px; right: -2px; width: 14px; height: 14px;
    border-top: 2px solid var(--sb-cyan); border-right: 2px solid var(--sb-cyan); pointer-events: none; }
.sb-hero-wrap::after, .sb-prog-card::after { content: ''; position: absolute; bottom: -2px; left: -2px; width: 14px; height: 14px;
    border-bottom: 2px solid var(--sb-cyan); border-left: 2px solid var(--sb-cyan); pointer-events: none; }
.sb-main-title { font-family: 'Changa', 'Cairo', sans-serif !important; letter-spacing: .02em; }
.sb-prog-fill { background-size: 200% 100%; animation: sbShine 1.5s linear infinite; box-shadow: 0 0 14px rgba(56,189,248,.5); }
@keyframes sbShine { 0% { background-position: 0% 0; } 100% { background-position: -200% 0; } }
.sb-fill-violet { background: linear-gradient(90deg,#7C3AED,#A855F7 55%,#E9D5FF); background-size: 200% 100%; box-shadow: 0 0 14px rgba(139,92,246,.55); }
.sb-grind { margin-top: 11px; padding-top: 10px; border-top: 1px dashed rgba(148,163,184,.3); }
.sb-grind-head { display: flex; align-items: center; gap: 10px; margin-bottom: 7px; flex-wrap: wrap; }
.sb-rank-live { font-family: 'JetBrains Mono', monospace; font-size: 12px; font-weight: 900; color: #030309;
    background: linear-gradient(90deg,#0EA5E9,#7DD3FC); padding: 2px 12px; letter-spacing: .06em; }
.sb-grind-label { font-size: 12.5px; font-weight: 800; color: #F8FAFC; }
.sb-grind-sub { font-size: 10.5px; color: var(--sb-muted); font-family: 'JetBrains Mono', 'Cairo', monospace; }
#sb-step1-btn, #sb-batch-btn { border-radius: 0 !important;
    clip-path: polygon(12px 0, 100% 0, 100% calc(100% - 12px), calc(100% - 12px) 100%, 0 100%, 0 12px); }
#sb-flash { position: fixed; inset: 0; background: radial-gradient(circle at 50% 45%, rgba(168,85,247,.55), rgba(3,3,9,0) 58%);
    opacity: 0; pointer-events: none; z-index: 10001; }
#sb-flash.go { animation: sbFlashGo 1.1s ease-out; }
@keyframes sbFlashGo { 0% { opacity: 0; } 10% { opacity: 1; } 100% { opacity: 0; } }
#sb-arise { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; z-index: 10002; pointer-events: none;
    font-family: 'Changa', 'Cairo', sans-serif; font-weight: 800; font-size: 110px; letter-spacing: .12em; direction: ltr;
    color: #E9D5FF; text-shadow: 0 0 40px rgba(168,85,247,1), 0 0 110px rgba(139,92,246,.8); opacity: 0; }
#sb-arise.go { animation: sbAriseIn 1.25s cubic-bezier(.16,1,.3,1); }
@keyframes sbAriseIn { 0% { opacity: 0; transform: scale(.5) translateY(26px); filter: blur(6px); }
    22% { opacity: 1; transform: scale(1.08); filter: blur(0); } 55% { opacity: 1; transform: scale(1); }
    100% { opacity: 0; transform: scale(1.04) translateY(-14px); } }
.gradio-container.sb-shake { animation: sbPageShake .5s linear; }
@keyframes sbPageShake { 0%,100% { transform: translate(0,0); } 15% { transform: translate(-7px,3px); }
    30% { transform: translate(6px,-4px); } 45% { transform: translate(-5px,-2px); } 60% { transform: translate(5px,3px); } 80% { transform: translate(-3px,1px); } }
#sb-sysalert { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; z-index: 10003; pointer-events: none; }
#sb-sysalert .win { min-width: 360px; text-align: center; padding: 18px 32px; background: rgba(8,12,28,.94);
    border: 1px solid var(--sb-cyan); box-shadow: 0 0 44px rgba(56,189,248,.45), inset 0 0 30px rgba(56,189,248,.12);
    opacity: 0; transform: scale(.7); }
#sb-sysalert .cap { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; letter-spacing: .24em; color: var(--sb-cyan-bright); margin-bottom: 6px; }
#sb-sysalert .big { font-family: 'Changa', 'Cairo', sans-serif; font-size: 40px; font-weight: 800; color: #FDE68A; text-shadow: 0 0 30px rgba(253,230,138,.85); }
#sb-sysalert .sub { font-family: 'JetBrains Mono', monospace; font-size: 15px; color: #FDE68A; margin-top: 4px; direction: ltr; }
#sb-sysalert.go .win { animation: sbAlertIn 1.6s cubic-bezier(.16,1,.3,1); }
@keyframes sbAlertIn { 0% { opacity: 0; transform: scale(.65); } 14% { opacity: 1; transform: scale(1.05); } 22% { opacity: .3; }
    30% { opacity: 1; transform: scale(1); } 80% { opacity: 1; } 100% { opacity: 0; transform: scale(1.02) translateY(-10px); } }
.sb-fps { color: #FDE68A; font-weight: 800; }

/* ==== Dark dropdowns (all Gradio dropdowns) ==== */
input[role="combobox"] {
    background: #0b1020 !important;
    color: #dfe8ff !important;
    border: 1px solid #2a3a66 !important;
}
input[role="combobox"]::placeholder { color: #5b6b96 !important; }
.options {
    background: #0b1020 !important;
    border: 1px solid #2a3a66 !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.65) !important;
}
ul.option-list { background: #0b1020 !important; }
ul.option-list li.item {
    background: transparent !important;
    color: #dfe8ff !important;
}
ul.option-list li.item:hover,
ul.option-list li.item.active {
    background: #1a2547 !important;
}
ul.option-list li.item.selected {
    background: #14203f !important;
    color: #8ab4ff !important;
}
.dropdown-arrow path { fill: #8ab4ff !important; }

/* dropdown wrapper shells (input itself can be transparent) */
div.wrap:has(input[role="combobox"]) .wrap-inner,
div.wrap:has(input[role="combobox"]) .secondary-wrap {
    background: #0b1020 !important;
    border-color: #2a3a66 !important;
}
.options input,
.options .wrap-inner,
.options .secondary-wrap {
    background: #0b1020 !important;
    color: #dfe8ff !important;
    border-color: #2a3a66 !important;
}

/* outer block ring around dropdowns */
div.block:has(input[role="combobox"]) {
    border-color: #2a3a66 !important;
}

"""

SHADOW_BLADE_HERO_HTML = """
<div class="sb-hero-wrap">
    <div class="sb-hero-inner">
        <div class="sb-brand-side">
            <div class="sb-emblem-box">
                <svg viewBox="0 0 64 64" width="42" height="42" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <polygon points="32,4 58,18 58,46 32,60 6,46 6,18" stroke="#A855F7" stroke-width="1.8" fill="#070D1D"/>
                    <path d="M32 12 C24 24 19 30 19 41 a13 13 0 0 0 26 0 C45 30 40 24 32 12 Z" fill="none" stroke="#A855F7" stroke-width="2.2"/>
                    <path d="M32 25 c-4.5 7 -7 10.5 -7 16 a7 7 0 0 0 14 0 c0 -5.5 -2.5 -9 -7 -16 Z" fill="#8B5CF6"/>
                </svg>
            </div>
            <div>
                <div class="sb-title-eyebrow">ANIME SUBBER · DEEPSEEK V4.1</div>
                <h1 class="sb-main-title">
                    سَبّر الأنمي
                    <span class="sb-rank-pill">AUTO-WHISPER · %W/%H RESPONSIVE</span>
                </h1>
                <p class="sb-subtitle-desc">
                    <strong>1 [STRIKE]:</strong> ترجمة إنجليزية ← عربية بأسلوب Celestial-Dragons عبر DeepSeek، ولو الحلقة خام بيسمعها Whisper ويفرّغ صوتها، ثم حرق وضغط كل الجودات MP4 &nbsp;←&nbsp;
                    <strong>2 [S-PORTAL]:</strong> الرفع السحابي (DoodStream / Google Drive)
                </p>
            </div>
        </div>
        <div class="sb-hud-chip">
            <span class="sb-dot-online"></span>
            <span>SYSTEM ONLINE</span>
            <span class="sb-fps" id="sb-fps-meter">-- FPS</span>
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


def _grind_rank(overall: float) -> str:
    if overall >= 99.5:
        return "S+"
    if overall >= 80:
        return "S"
    if overall >= 60:
        return "A"
    if overall >= 40:
        return "B"
    if overall >= 20:
        return "C"
    return "D"


def render_progress_hud(
    pct: float = 0.0,
    badge: str = "HUNTER HUD · STANDBY",
    title: str = "جاهز — اضغط [STRIKE] للترجمة والحرق والضغط MP4",
    metric: str = "السرعة: — · المتبقي: —",
    detail: str = "الحلقات الخام والقواميس والموديلات محفوظة دائماً في /workspace",
    mode: str = "cyan",
    overall_pct: Optional[float] = None,
    grind_label: str = "",
) -> str:
    clamped = max(0.0, min(100.0, float(pct)))
    mode_cls = {"violet": "sb-mode-violet", "done": "sb-mode-done", "error": "sb-mode-error"}.get(mode, "")
    grind_html = ""
    if overall_pct is not None:
        ov = max(0.0, min(100.0, float(overall_pct)))
        rank = _grind_rank(ov)
        grind_html = f"""
        <div class="sb-grind">
            <div class="sb-grind-head">
                <span class="sb-rank-live" id="sb-rank-live">RANK {rank}</span>
                <span class="sb-grind-label">{html.escape(grind_label)}</span>
                <span class="sb-grind-sub">إجمالي الموسم {ov:.1f}%</span>
            </div>
            <div class="sb-prog-track">
                <div class="sb-prog-fill sb-fill-violet" style="width: {ov:.1f}%;"></div>
            </div>
        </div>
        """
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
        {grind_html}
    </div>
    """


def load_settings() -> Dict:
    defaults = {
        "provider": "DeepSeek API (موصى به)",
        "llm_model": "deepseek-v4-pro",
        "api_key": os.environ.get("DEEPSEEK_API_KEY", os.environ.get("GEMINI_API_KEY", "")),
        "base_url": "https://api.deepseek.com",
        "style_preset": "bleach_style",
        "wrap_mode": "sequential",
        "soften_exclaim": True,
        "dood_api_key": os.environ.get("DOODSTREAM_API_KEY", ""),
        "gdrive_remote": "gdrive",
        "gdrive_folder": "Runpod_Anime",
        "codec": "hevc_nvenc",
        "preset": "p7",
        "cq": 26,
        "audio_bitrate": "160k",
        "credit_translator": "",
        "credit_typesetter": "",
        "credit_encoder": "",
        "logo_position": "top-right",
        "logo_opacity": 0.30,
        "logo_scale": 0.12,
    }
    if SETTINGS_FILE.is_file():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update({k: v for k, v in saved.items() if v is not None})
        except Exception:
            pass
    return defaults


_drive_restore_glossaries()


def save_settings(
    provider, llm_model, api_key, base_url, style_preset, wrap_mode, soften_exclaim, dood_api_key,
    gdrive_remote, gdrive_folder,
    codec, preset, cq, audio_bitrate,
    credit_translator, credit_typesetter, credit_encoder,
    logo_position, logo_opacity, logo_scale,
) -> str:
    data = {
        "provider": provider,
        "llm_model": llm_model,
        "api_key": api_key,
        "base_url": base_url,
        "style_preset": style_preset,
        "wrap_mode": wrap_mode,
        "soften_exclaim": bool(soften_exclaim),
        "dood_api_key": dood_api_key,
        "gdrive_remote": gdrive_remote,
        "gdrive_folder": gdrive_folder,
        "codec": codec,
        "preset": preset,
        "cq": int(cq),
        "audio_bitrate": audio_bitrate,
        "credit_translator": credit_translator,
        "credit_typesetter": credit_typesetter,
        "credit_encoder": credit_encoder,
        "logo_position": logo_position,
        "logo_opacity": float(logo_opacity),
        "logo_scale": float(logo_scale),
    }
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"✅ تم حفظ جميع المفاتيح والإعدادات الدائمة في `{SETTINGS_FILE}`."


def _on_provider_change(prov: str) -> Tuple[Any, Any]:
    if "DeepSeek" in prov:
        return gr.update(value="deepseek-v4-pro"), gr.update(value="https://api.deepseek.com")
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


RUN_LOG_FILE = STORAGE_ROOT / "_logs" / "hunter_log.txt"


def _append_disk_log(line: str) -> None:
    try:
        RUN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RUN_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def list_local_outputs() -> List[str]:
    try:
        if not STORAGE_ROOT.exists():
            return []
        mp4s = sorted(STORAGE_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [str(p) for p in mp4s if not p.name.endswith("_preview.mp4")][:50]
    except OSError:
        return []


def _browser_preview_path(video_path: str) -> str:
    """Prefer the fast H.264 browser copy (..._preview.mp4) for the preview player,
    so picking a burned episode never triggers Gradio's slow CPU re-encode."""
    try:
        if video_path:
            cand = Path(video_path).with_name(Path(video_path).stem + "_preview.mp4")
            if cand.is_file():
                return str(cand)
    except Exception:
        pass
    return video_path


def list_cached_raw_videos() -> List[str]:
    try:
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
    except OSError:
        return []


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


def step1_process_for_preview(
    episode_name: str,
    series_name: str,
    series_pick: str,
    style_preset: str,
    wrap_mode: str,
    soften_exclaim: bool,
    saved_raw_1080: str,
    link_1080: str, file_1080: Optional[str],
    link_720: str, file_720: Optional[str],
    link_480: str, file_480: Optional[str],
    sub_mode: str,
    whisper_model: str,
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
    logo_position: str,
    logo_opacity: float,
    logo_scale: float,
):
    logs: List[str] = []
    rendered_map: Dict[str, str] = {}
    ass_out: Optional[str] = None
    if series_pick and series_pick != SERIES_NEW_OPT:
        series_name = series_pick

    def push(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        logs.append(line)
        _append_disk_log(line)
        print(line, flush=True)

    push(f"🗂️ نسخة دائمة من هذا السجل تُحفظ تلقائياً في: {RUN_LOG_FILE}")

    def pack(
        hud_html: str,
        preview_val=gr.update(),
        local_dd=gr.update(),
        raw_dd=gr.update(),
        vids_val=gr.update(),
        ass_val=gr.update(),
        state_val=gr.update(),
        g1=gr.update(),
        g2=gr.update(),
        dls=gr.update(),
        lnk=gr.update(),
    ):
        return (hud_html, "\n".join(logs[-150:]), preview_val, local_dd, raw_dd, vids_val, ass_val, state_val, g1, g2, dls, lnk)

    grind_ctx = {"gi": 0, "total": 0}

    def run_live(fn: Callable, *args, badge: str, title: str, mode: str = "cyan", accepts_progress_cb: bool = True, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        if accepts_progress_cb:
            kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except BaseException as e:
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
                ov_pct, ov_label = None, ""
                if grind_ctx["total"]:
                    _gi, _tot = grind_ctx["gi"], grind_ctx["total"]
                    ov_pct = ((_gi - 1) + max(0.0, min(100.0, cur_pct)) / 100.0) / _tot * 100.0
                    ov_label = f"طحن الموسم: الحلقة {_gi} من {_tot}"
                yield pack(render_progress_hud(cur_pct, badge, title, cur_met, cur_det, mode=mode, overall_pct=ov_pct, grind_label=ov_label))

        if "err" in res_box:
            raise res_box["err"]  # type: ignore[misc]
        return res_box.get("val")

    eff_1080 = saved_raw_1080 if (saved_raw_1080 and os.path.isfile(saved_raw_1080)) else file_1080
    quality_inputs = [
        (lbl, lnk, fl)
        for lbl, lnk, fl in [("1080p", link_1080, eff_1080), ("720p", link_720, file_720), ("480p", link_480, file_480)]
        if (lnk and lnk.strip()) or fl
    ]
    stale_uploads = [lbl for lbl, _lnk, fl in quality_inputs if fl and not os.path.isfile(fl)]
    if stale_uploads:
        push(f"⚠️ ملفات الرفع المؤقتة لجودة {'/'.join(stale_uploads)} اتمسحت من السيرفر (الرفع المؤقت بيعيش ساعة تقريباً) — ارفع الملف تاني أو اختر الحلقة من قائمة المحفوظ.")
        yield pack(render_progress_hud(0.0, "تنبيه · أعد رفع الملف", "الملف المرفوع انتهت صلاحيته — ارفعه تاني أو اختر من المحفوظ على السيرفر", "—", "—", mode="error"), state_val={})
        return
    if not quality_inputs:
        push("⚠️ أدخل رابط تورنت أو اختر حلقة خام محفوظة من القائمة قبل الانطلاق.")
        yield pack(render_progress_hud(0.0, "تنبيه · لا يوجد مصدر", "أدخل رابط أو اختر حلقة محفوظة", "—", "—", mode="error"), state_val={})
        return

    auto_base_name = episode_name.strip() or (Path(eff_1080).stem if (eff_1080 and os.path.isfile(eff_1080)) else "")
    ep_folder_name = _safe_name(auto_base_name)
    ep_out_dir = STORAGE_ROOT / ep_folder_name
    work_dir = RAW_CACHE_ROOT / "_work" / ep_folder_name
    try:
        ep_out_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        push(f"❌ تعذر تجهيز مجلدات العمل على السيرفر: {exc}\n{traceback.format_exc()[-800:]}")
        yield pack(
            render_progress_hud(0.0, "ERROR · STORAGE", f"مشكلة في مساحة التخزين: {str(exc)[:110]}", "راجع مساحة /workspace على السيرفر", "—", mode="error"),
            state_val={},
        )
        return

    yield pack(
        render_progress_hud(0.0, "INIT · PREPARING", "تم تجهيز مساحة العمل — بدء التحميل...", "—", "—"),
        g1=gr.update(visible=False),
        lnk=gr.update(visible=False),
    )

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

        # مصدر الترجمة: سحب الترجمة الإنجليزية المدمجة، وإلا تفريغ صوتي بـ Whisper
        source_sub = None
        is_raw_whisper = False
        if sub_mode.startswith("Auto"):
            probe = probe_embedded_subtitles(str(primary_vid))
            if probe is not None:
                source_sub = extract_embedded_subtitle(str(primary_vid), probe[0], probe[1], str(work_dir))
                push(f"✓ تم سحب الترجمة المدمجة ({probe[1]}).")
        if source_sub is None:
            is_raw_whisper = True
            source_sub = yield from run_live(
                transcribe_with_whisper,
                str(primary_vid),
                str(work_dir / "whisper.srt"),
                model_size=whisper_model,
                badge=f"STAGE 2/4 · WHISPER [{whisper_model}]",
                title="حلقة خام بدون ترجمة — تفريغ الصوت بـ Faster-Whisper مع طوابع الكلمات...",
                accepts_progress_cb=False,
            )

        entries = parse_source_subtitles(source_sub)

        glossary: Dict = {}
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
                badge="STAGE 2/4 · SERIES GLOSSARY",
                title="تجهيز قاموس أسماء ومصطلحات الأنمي...",
                accepts_progress_cb=False,
            )

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

        glossary = yield from run_live(
            learn_glossary_from_translation,
            series_name=series_name,
            entries=entries,
            translations=translations,
            glossary=glossary,
            provider=_provider_key(provider),
            api_key=api_key,
            model_name=llm_model,
            base_url=base_url,
            badge="STAGE 3/4 · GLOSSARY LEARN",
            title="حفظ أسماء ومصطلحات الحلقة في قاموس الأنمي...",
            accepts_progress_cb=False,
        )
        if _drive_backup_glossary(series_name):
            push("📚☁️ قاموس أسماء «" + (series_name.strip() or "الأنمي") + "» اتحفظ على Drive — الأسماء هتفضل ثابتة حتى لو قفلت السيرفر وبدأت من الأول.")

        credits_dict = {
            "translator": credit_translator,
            "typesetter": credit_typesetter,
            "encoder": credit_encoder,
        }
        has_credits = any(v.strip() for v in credits_dict.values())

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
                credits=credits_dict if has_credits else None,
                wrap_mode=wrap_mode,
                soften_exclaim=soften_exclaim,
            )
            if ass_out is None:
                ass_out = q_ass
            push(f"📐 تم توليد ملف ASS نسبي لجودة {q_label} ({vw}x{vh})")

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
                logo_path=str(LOGO_FILE) if LOGO_FILE.is_file() else "",
                logo_position=logo_position,
                logo_opacity=float(logo_opacity),
                logo_scale=float(logo_scale),
                badge=f"STAGE 4/4 · NVENC ENCODE [{q_label}]",
                title=f"حرق الترجمة وضغط جودة {q_label} بأعلى جودة وأقل حجم ({codec})...",
                accepts_progress_cb=True,
            )
            yield from run_live(
                qc_burned_episode,
                str(vid_path), out_mp4,
                ass_path=q_ass,
                expected_lines=len(translations),
                qc_dir=str(ep_out_dir / "_qc" / q_label),
                badge=f"QC [{q_label}]",
                title=f"فحص جودة نسخة {q_label} بعد الحرق...",
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
        preview_file = first_video
        try:
            preview_file = yield from run_live(
                make_browser_preview,
                first_video,
                str(Path(first_video).with_name(Path(first_video).stem + "_preview.mp4")),
                badge="PREVIEW · H264",
                title="تجهيز نسخة معاينة سريعة للمتصفح (H.264 على كرت الشاشة)...",
                accepts_progress_cb=True,
            )
        except Exception as pv_exc:
            push(f"⚠️ تعذر تجهيز نسخة المعاينة السريعة: {pv_exc} — المعاينة هتستخدم الملف الأصلي.")
            preview_file = first_video
        all_local = list_local_outputs()

        push(f"✅ اكتمل الإنتاج بالكامل! عاين الفيديو الآن، ثم اضغط [S-PORTAL] للرفع السحابي.")
        done_hud = render_progress_hud(
            100.0,
            "MISSION COMPLETE · 100%",
            "اكتملت الترجمة والحرق والضغط بنجاح! الفيديو جاهز للمعاينة",
            f"تم إنتاج {len(rendered_map)} جودة MP4",
            str(ep_out_dir),
            mode="done",
        )
        # التحديث الأول بدون فيديو المعاينة: ظهور لوحة الإتمام وزرار [S-PORTAL] فوراً،
        # لأن تحويل Gradio لملف HEVC إلى mp4 للمتصفح كان يحبس التحديث كله دقائق طويلة
        yield pack(
            done_hud,
            local_dd=gr.update(choices=all_local),
            raw_dd=gr.update(choices=updated_raw_list),
            vids_val=list(rendered_map.values()),
            ass_val=ass_out,
            state_val={"folder": str(ep_out_dir), "files": rendered_map},
            g2=gr.update(visible=True),
            dls=gr.update(visible=True),
        )
        # المعاينة تصل في تحديث منفصل (نسخة H.264 جاهزة مسبقاً فتظهر فوراً بدون تحويل)
        yield pack(
            done_hud,
            preview_val=preview_file,
            local_dd=gr.update(value=first_video),
        )
    except Exception as exc:
        push(f"❌ حدث خطأ أثناء التجهيز: {exc}\n{traceback.format_exc()[-1500:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        try:
            err_hud = render_progress_hud(
                0.0,
                "ERROR · SYSTEM HALTED",
                f"توقف بسبب خطأ: {str(exc)[:110]}",
                "نقطة حفظ الترجمة والملف الخام ما زالا محفوظين",
                "راجع سجل العمليات",
                mode="error",
            )
        except Exception:
            err_hud = ""
        try:
            raw_choices = [""] + list_cached_raw_videos()
        except Exception:
            raw_choices = [""]
        try:
            done_vids = list(rendered_map.values()) or None
        except Exception:
            done_vids = None
        try:
            ass_safe = ass_out
        except Exception:
            ass_safe = None
        yield pack(
            err_hud,
            raw_dd=gr.update(choices=raw_choices),
            vids_val=done_vids,
            ass_val=ass_safe,
            state_val={},
            g1=gr.update(visible=True),
        )
    except BaseException as exc:
        push(f"🛑 توقف مفاجئ خارج معالجة الأخطاء ({type(exc).__name__}): {exc}\n{traceback.format_exc()[-800:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        raise


def step_batch_process(
    batch_links: str,
    series_name: str,
    series_pick: str,
    style_preset: str,
    wrap_mode: str,
    soften_exclaim: bool,
    sub_mode: str,
    whisper_model: str,
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
    logo_position: str,
    logo_opacity: float,
    logo_scale: float,
):
    """Season batch mode: one or more torrent links (each may hold a whole season or more),
    download all episodes, translate + burn + compress each, and collect them into one
    folder split into sub-folders of 35 episodes for long anime. Resume-safe."""
    logs: List[str] = []

    def push(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        logs.append(line)
        _append_disk_log(line)
        print(line, flush=True)

    push(f"🗂️ نسخة دائمة من هذا السجل تُحفظ تلقائياً في: {RUN_LOG_FILE}")

    def pack(
        hud_html: str,
        preview_val=gr.update(),
        local_dd=gr.update(),
        raw_dd=gr.update(),
        vids_val=gr.update(),
        ass_val=gr.update(),
        state_val=gr.update(),
        g1=gr.update(),
        g2=gr.update(),
        dls=gr.update(),
        lnk=gr.update(),
    ):
        return (hud_html, "\n".join(logs[-150:]), preview_val, local_dd, raw_dd, vids_val, ass_val, state_val, g1, g2, dls, lnk)

    def run_live(fn: Callable, *args, badge: str, title: str, mode: str = "cyan", accepts_progress_cb: bool = True, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        if accepts_progress_cb:
            kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except BaseException as e:
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

    if series_pick and series_pick != SERIES_NEW_OPT:
        series_name = series_pick
    links = [ln.strip() for ln in (batch_links or "").splitlines() if ln.strip()]
    if not links:
        push("⚠️ أدخل رابط تورنت واحد على الأقل (رابط في كل سطر).")
        yield pack(render_progress_hud(0.0, "تنبيه · لا توجد روابط", "أدخل روابط التورنت أولاً", "—", "—", mode="error"), state_val={})
        return

    series_safe = _safe_name(series_name.strip() or "Anime_Season")
    series_root = STORAGE_ROOT / series_safe
    ass_root = series_root / "_ass_files"
    try:
        series_root.mkdir(parents=True, exist_ok=True)
        ass_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        push(f"❌ تعذر تجهيز مجلدات الموسم على السيرفر: {exc}\n{traceback.format_exc()[-800:]}")
        yield pack(
            render_progress_hud(0.0, "ERROR · STORAGE", f"مشكلة في مساحة التخزين: {str(exc)[:110]}", "راجع مساحة /workspace على السيرفر", "—", mode="error"),
            state_val={},
        )
        return

    yield pack(
        render_progress_hud(0.0, "INIT · PREPARING", "تم تجهيز مساحة العمل — بدء طحن الموسم...", "—", "—"),
        g1=gr.update(visible=False),
        lnk=gr.update(visible=False),
    )


    try:
        # ═══ المرحلة 1: تحميل كل الروابط وجمع كل الحلقات ═══
        all_eps: List[Path] = []
        for idx, lnk in enumerate(links, 1):
            cdir = _raw_cache_dir_for_source("season", lnk, None, f"{series_safe}_{idx}")
            vids = yield from run_live(
                download_season_batch,
                lnk, None, str(cdir),
                badge=f"SEASON DOWNLOAD [{idx}/{len(links)}]",
                title=f"تحميل / فحص الدفعة رقم {idx} من {len(links)} (ممكن تحتوي موسماً كاملاً)",
                accepts_progress_cb=True,
            )
            all_eps.extend(vids)

        unique = {str(p.resolve()): p for p in all_eps}
        all_eps = sorted(unique.values(), key=episode_sort_key)
        total = len(all_eps)
        if total == 0:
            raise FileNotFoundError("لم يتم العثور على أي حلقة في الروابط المدخلة.")

        grind_ctx["total"] = total
        push(f"📚 إجمالي الحلقات المكتشفة: {total} حلقة.")
        for pos, vp in enumerate(all_eps, 1):
            epn = extract_episode_number(vp.name)
            push(f"   {pos:03d}. {vp.name}" + (f"  (حلقة {epn})" if epn is not None else ""))

        # ═══ هيكل الإخراج: فولدر واحد، يتقسم لفولدرات 35 حلقة للأنمي الطويل ═══
        split_parts = total > 35

        def part_dir_for(global_i: int) -> Path:
            if not split_parts:
                return series_root
            part = (global_i - 1) // 35 + 1
            lo = (part - 1) * 35 + 1
            hi = min(part * 35, total)
            d = series_root / f"Part {part:02d} (EP{lo:03d}-{hi:03d})"
            d.mkdir(parents=True, exist_ok=True)
            return d

        if split_parts:
            n_parts = (total + 34) // 35
            push(f"🗂️ أنمي طويل ({total} حلقة): سيتم التقسيم إلى {n_parts} فولدرات × 35 حلقة.")

        credits_dict = {
            "translator": credit_translator,
            "typesetter": credit_typesetter,
            "encoder": credit_encoder,
        }
        has_credits = any(v.strip() for v in credits_dict.values())

        rendered: Dict[str, str] = {}
        failed: List[str] = []
        glossary: Dict = {}
        last_ass: Optional[str] = None

        # ☁️ نسخ احتياطي على Google Drive: البود مؤقت وكل حاجة بتتمسح لما يتقفل
        _st = load_settings()
        drv_remote = str(_st.get("gdrive_remote", "gdrive")).strip() or "gdrive"
        drv_folder = str(_st.get("gdrive_folder", "Runpod_Anime")).strip() or "Runpod_Anime"
        drive_ok = rclone_remote_configured(drv_remote)
        if drive_ok:
            push("☁️ Google Drive متربط — هيتم رفع كل حلقة أول بأول + حفظ الذاكرة والقاموس.")
            drive_download_file(drv_remote, f"{drv_folder}/_state/{series_safe}", "_translation_memory.json", str(series_root / "_translation_memory.json"))
            if drive_download_file(drv_remote, f"{drv_folder}/_state/glossaries", f"{_glossary_slug(series_name)}.json", str(_glossary_dir() / f"{_glossary_slug(series_name)}.json")):
                push("♻️ تم استرجاع قاموس الأنمي من Drive — الأسماء والمصطلحات زي ما وصلتله آخر مرة.")
        else:
            push("⚠️ Google Drive مش متربط (rclone) — الشغل هيتحفظ محلياً بس وهيتمسح لو البود اتقفل!")

        # ذاكرة ترجمة الموسم: الأسطر المكررة (الأوبنينج/الإندينج) تترجم مرة واحدة فقط
        tmem_path = series_root / "_translation_memory.json"
        tmem: Dict[str, str] = {}
        if tmem_path.is_file():
            try:
                tmem = {str(k): str(v) for k, v in json.loads(tmem_path.read_text(encoding="utf-8")).items()}
            except Exception:
                tmem = {}
        if tmem:
            push(f"♻️ ذاكرة ترجمة الموسم محملة: {len(tmem)} سطر جاهز لإعادة الاستخدام.")

        def _tkey(txt: str) -> str:
            return " ".join((txt or "").split())

        # ═══ المرحلة 2: معالجة كل حلقة (ترجمة ← حرق ← ضغط) ═══
        for gi, vid in enumerate(all_eps, 1):
            ep_label = f"EP{gi:03d}"
            grind_ctx["gi"] = gi
            out_mp4 = part_dir_for(gi) / f"{series_safe}_{ep_label}_AR.mp4"

            if out_mp4.exists() and out_mp4.stat().st_size > 1024 * 1024:
                push(f"⏭️ {ep_label} جاهزة مسبقاً — تخطي ({out_mp4.name})")
                rendered[ep_label] = str(out_mp4)
                continue

            push(f"━━━━━━ {ep_label} ({gi}/{total}): {vid.name} ━━━━━━")
            work_dir = RAW_CACHE_ROOT / "_work" / series_safe / ep_label
            work_dir.mkdir(parents=True, exist_ok=True)

            try:
                source_sub = None
                is_raw_whisper = False
                if sub_mode.startswith("Auto"):
                    probe = probe_embedded_subtitles(str(vid))
                    if probe is not None:
                        source_sub = extract_embedded_subtitle(str(vid), probe[0], probe[1], str(work_dir))
                        push(f"✓ {ep_label}: تم سحب الترجمة المدمجة ({probe[1]}).")
                if source_sub is None:
                    is_raw_whisper = True
                    source_sub = yield from run_live(
                        transcribe_with_whisper,
                        str(vid),
                        str(work_dir / "whisper.srt"),
                        model_size=whisper_model,
                        badge=f"WHISPER [{ep_label} {gi}/{total}]",
                        title=f"حلقة {gi} من {total} خام بدون ترجمة — تفريغ الصوت...",
                        accepts_progress_cb=False,
                    )

                entries = parse_source_subtitles(source_sub)

                if (api_key.strip() or "Local" in provider) and not glossary:
                    glossary = yield from run_live(
                        load_or_extract_series_glossary,
                        series_name=series_name,
                        entries=entries,
                        is_raw_whisper=is_raw_whisper,
                        provider=_provider_key(provider),
                        api_key=api_key,
                        model_name=llm_model,
                        base_url=base_url,
                        badge=f"SERIES GLOSSARY [{ep_label}]",
                        title="تجهيز قاموس أسماء ومصطلحات الأنمي (مرة واحدة لكل الموسم)...",
                        accepts_progress_cb=False,
                    )

                reused: Dict[int, str] = {}
                if tmem:
                    for e in entries:
                        if getattr(e, "keep_untranslated", False):
                            continue
                        k = _tkey(getattr(e, "plain_text", ""))
                        if k and k in tmem:
                            reused[e.index] = tmem[k]
                if reused:
                    push(f"♻️ {ep_label}: {len(reused)} سطر مكرر (أوبنينج/إندينج) أُعيد استخدام ترجمته — المتبقي {len(entries) - len(reused)} سطر.")

                translations = yield from run_live(
                    translate_entries,
                    entries=[e for e in entries if e.index not in reused],
                    provider=_provider_key(provider),
                    api_key=api_key,
                    model_name=llm_model,
                    base_url=base_url,
                    series_name=series_name,
                    glossary=glossary,
                    checkpoint_path=str(work_dir / "translation_checkpoint.json"),
                    badge=f"TRANSLATION [{ep_label} {gi}/{total}]",
                    title=f"ترجمة حلقة {gi} من {total} بأسلوب Celestial-Dragons ({len(entries) - len(reused)} سطر)...",
                    accepts_progress_cb=False,
                )
                translations.update(reused)

                idx2key = {
                    e.index: _tkey(getattr(e, "plain_text", ""))
                    for e in entries
                    if not getattr(e, "keep_untranslated", False)
                }
                for _idx, _ar in translations.items():
                    _k = idx2key.get(_idx)
                    if _k:
                        tmem[_k] = _ar
                try:
                    tmem_path.write_text(json.dumps(tmem, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

                glossary = yield from run_live(
                    learn_glossary_from_translation,
                    series_name=series_name,
                    entries=entries,
                    translations=translations,
                    glossary=glossary,
                    provider=_provider_key(provider),
                    api_key=api_key,
                    model_name=llm_model,
                    base_url=base_url,
                    badge=f"GLOSSARY LEARN [{ep_label}]",
                    title="حفظ أسماء ومصطلحات الحلقة في قاموس الأنمي...",
                    accepts_progress_cb=False,
                )

                vw, vh, _, _ = probe_video_info(str(vid))
                ep_ass = ass_root / f"{series_safe}_{ep_label}_AR.ass"
                write_responsive_ass(
                    entries=entries,
                    translations=translations,
                    out_ass_path=str(ep_ass),
                    target_w=vw,
                    target_h=vh,
                    style_preset=style_preset,
                    credits=credits_dict if has_credits else None,
                    wrap_mode=wrap_mode,
                    soften_exclaim=soften_exclaim,
                )
                last_ass = str(ep_ass)

                yield from run_live(
                    burn_and_compress_native,
                    video_path=str(vid),
                    ass_path=str(ep_ass),
                    output_path=str(out_mp4),
                    fonts_dir=str(FONTS_DIR),
                    codec=codec,
                    cq=int(cq),
                    preset=preset,
                    audio_bitrate=audio_bitrate,
                    logo_path=str(LOGO_FILE) if LOGO_FILE.is_file() else "",
                    logo_position=logo_position,
                    logo_opacity=float(logo_opacity),
                    logo_scale=float(logo_scale),
                    badge=f"ENCODE [{ep_label} {gi}/{total}]",
                    title=f"حرق وضغط حلقة {gi} من {total} ({codec})...",
                    accepts_progress_cb=True,
                )
                yield from run_live(
                    qc_burned_episode,
                    str(vid), str(out_mp4),
                    ass_path=str(ep_ass),
                    expected_lines=len(entries),
                    qc_dir=str(series_root / "_qc" / ep_label),
                    badge=f"QC [{ep_label} {gi}/{total}]",
                    title=f"فحص جودة حلقة {gi} من {total} بعد الحرق...",
                    accepts_progress_cb=True,
                )
                rendered[ep_label] = str(out_mp4)
                if drive_ok:
                    try:
                        yield from run_live(
                            upload_to_rclone,
                            str(out_mp4), drv_remote, f"{drv_folder}/{series_safe}",
                            generate_link=False,
                            badge=f"DRIVE BACKUP [{ep_label} {gi}/{total}]",
                            title=f"نسخ احتياطي لحلقة {gi} من {total} على Google Drive...",
                            accepts_progress_cb=True,
                        )
                        for _sf, _sub in ((tmem_path, f"_state/{series_safe}"), (_glossary_dir() / f"{_glossary_slug(series_name)}.json", "_state/glossaries")):
                            if _sf.is_file():
                                yield from run_live(
                                    upload_to_rclone,
                                    str(_sf), drv_remote, f"{drv_folder}/{_sub}",
                                    generate_link=False,
                                    badge=f"STATE BACKUP [{ep_label}]",
                                    title="حفظ ذاكرة الترجمة والقاموس على Drive...",
                                    accepts_progress_cb=False,
                                )
                    except Exception as up_exc:
                        push(f"⚠️ فشل النسخ الاحتياطي لـ {ep_label} على Drive: {up_exc} — الحلقة محفوظة محلياً ومكملين.")
                shutil.rmtree(work_dir, ignore_errors=True)
                if cleanup_temp:
                    try:
                        vid.unlink(missing_ok=True)
                    except OSError:
                        pass
            except Exception as ep_exc:
                failed.append(f"{ep_label} ({vid.name}): {ep_exc}")
                push(f"❌ فشلت {ep_label}: {ep_exc} — متابعة باقي الحلقات...")
                continue

        all_local = list_local_outputs()
        first_video = next(iter(rendered.values()), None)
        preview_file = first_video
        if first_video:
            try:
                preview_file = yield from run_live(
                    make_browser_preview,
                    first_video,
                    str(Path(first_video).with_name(Path(first_video).stem + "_preview.mp4")),
                    badge="PREVIEW · H264",
                    title="تجهيز نسخة معاينة سريعة للمتصفح (H.264 على كرت الشاشة)...",
                    accepts_progress_cb=True,
                )
            except Exception as pv_exc:
                push(f"⚠️ تعذر تجهيز نسخة المعاينة السريعة: {pv_exc} — المعاينة هتستخدم الملف الأصلي.")
                preview_file = first_video
        done_n, fail_n = len(rendered), len(failed)

        if fail_n:
            push(f"⚠️ حلقات فشلت ({fail_n}):")
            for f_ in failed:
                push(f"   • {f_}")

        push(f"✅ اكتمل الموسم! {done_n}/{total} حلقة جاهزة في: {series_root}")
        done_hud = render_progress_hud(
            100.0 if not fail_n else 97.0,
            "SEASON COMPLETE" if not fail_n else "SEASON DONE WITH ERRORS",
            f"تم إنتاج {done_n} من {total} حلقة" + (f" · {fail_n} فشلت" if fail_n else " بنجاح كامل!"),
            f"الفولدر: {series_root}" + (" · مقسم ×35" if split_parts else ""),
            f"{done_n} MP4",
            mode="done" if not fail_n else "error",
            overall_pct=(done_n / total) * 100.0,
            grind_label=f"طحن الموسم: {done_n} من {total}",
        )
        # نفس فصل التحديثات: لوحة الإتمام وزرار [S-PORTAL] فوراً بدون انتظار تحويل المعاينة
        yield pack(
            done_hud,
            local_dd=gr.update(choices=all_local),
            raw_dd=gr.update(choices=[""] + list_cached_raw_videos()),
            vids_val=list(rendered.values())[:40],
            ass_val=last_ass,
            state_val={"folder": str(series_root), "files": rendered},
            g2=gr.update(visible=True),
            dls=gr.update(visible=True),
        )
        yield pack(
            done_hud,
            preview_val=preview_file,
            local_dd=gr.update(value=first_video),
        )
    except Exception as exc:
        push(f"❌ خطأ عام في وضع الموسم: {exc}\n{traceback.format_exc()[-1500:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        try:
            err_hud = render_progress_hud(
                0.0,
                "ERROR · SEASON HALTED",
                f"توقف الموسم بسبب خطأ: {str(exc)[:110]}",
                "الحلقات الجاهزة ونقاط الحفظ محفوظة — أعد التشغيل للاستكمال",
                "راجع سجل العمليات",
                mode="error",
            )
        except Exception:
            err_hud = ""
        try:
            raw_choices = [""] + list_cached_raw_videos()
        except Exception:
            raw_choices = [""]
        yield pack(
            err_hud,
            raw_dd=gr.update(choices=raw_choices),
            state_val={},
            g1=gr.update(visible=True),
        )
    except BaseException as exc:
        push(f"🛑 توقف مفاجئ خارج معالجة الأخطاء في وضع الموسم ({type(exc).__name__}): {exc}\n{traceback.format_exc()[-800:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        raise


def step2_approve_and_upload(
    job_state: Dict,
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
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        logs.append(line)
        _append_disk_log(line)
        print(line, flush=True)

    push(f"🗂️ نسخة دائمة من هذا السجل تُحفظ تلقائياً في: {RUN_LOG_FILE}")

    def run_upload_live(fn: Callable, *args, badge: str, title: str, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker():
            try:
                res_box["val"] = fn(*args, **kwargs)
            except BaseException as e:
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
            gr.update(value="\n".join(links_report), visible=True),
        )
    except Exception as exc:
        push(f"❌ خطأ أثناء الرفع: {exc}\n{traceback.format_exc()[-1500:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        try:
            err_hud = render_progress_hud(0.0, "S-PORTAL · ERROR", f"خطأ أثناء الرفع: {str(exc)[:100]}", "راجع السجل", "", mode="error")
        except Exception:
            err_hud = ""
        yield (
            err_hud,
            "\n".join(logs[-150:]),
            "\n".join(links_report),
        )
    except BaseException as exc:
        push(f"🛑 توقف مفاجئ أثناء الرفع ({type(exc).__name__}): {exc}\n{traceback.format_exc()[-800:]}\n🗂️ السجل الكامل محفوظ في: {RUN_LOG_FILE}")
        raise


def _build_blocks_kwargs() -> Dict:
    sig = inspect.signature(gr.Blocks.__init__).parameters
    kwargs: Dict = {"title": "سَبّر الأنمي | Anime Subber"}
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

        with gr.Row():
            with gr.Column(scale=2):
                with gr.Column(visible=True, elem_classes=["sb-wiz-step"]) as step1_group:
                    gr.HTML('<div style="font-weight:700;color:#9fe3ff;border:1px solid rgba(127,216,255,.35);background:rgba(20,40,70,.55);padding:8px 14px;border-radius:10px;margin:4px 0 10px;letter-spacing:.5px;font-size:15px;">⚔️ الخطوة 1 · جهّز الحلقة: اختر المصدر أو حلقة محفوظة ثم اضغط ARISE</div>')
                    with gr.Row():
                        episode_name = gr.Textbox(
                            label="📌 اسم الحلقة / مجلد الإخراج",
                            placeholder="مثال: Bleach_EP366 (أو اتركه فارغاً ليأخذ اسم الملف)",
                            scale=2,
                        )
                        with gr.Column(scale=2):
                            series_picker = gr.Dropdown(
                                choices=[(SERIES_NEW_OPT, SERIES_NEW_OPT)] + list_saved_series(),
                                value=_default_series_pick(),
                                label="🎌 الأنمي الشغال عليه — القاموس بيتحفظ وبيترجع لوحده",
                                elem_id="series_picker",
                            )
                            series_name = gr.Textbox(
                                label="✍️ اسم الأنمي الجديد (لتوحيد وحفظ قاموس الأسماء والمصطلحات تلقائياً)",
                                placeholder="مثال: Bleach أو Black Clover أو Jujutsu Kaisen",
                                visible=(_default_series_pick() == SERIES_NEW_OPT),
                            )
                        style_preset = gr.State("bleach_style")  # fixed: Bleach geometry, no UI control

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

                    with gr.Accordion("📚 وضع المواسم الكاملة — روابط دفعات (الرابط الواحد ممكن يحتوي موسماً أو أكثر)", open=False, elem_classes=["sb-accordion"]):
                        batch_links = gr.Textbox(
                            label="روابط التورنت / الماجنت — رابط واحد في كل سطر",
                            placeholder="magnet:?xt=urn:btih:...\nhttps://example.com/Season2.torrent",
                            lines=4,
                        )
                        batch_btn = gr.Button(
                            "🚀 شغّل الموسم كاملاً: تحميل كل الحلقات ← ترجمة بأسلوب المترجم ← حرق ← ضغط ← فولدرات 35 حلقة [SEASON STRIKE]",
                            variant="primary",
                            size="lg",
                            elem_id="sb-batch-btn",
                        )

                    with gr.Accordion("🔑 مفاتيح DeepSeek / Gemini والإعدادات الدائمة", open=False, elem_classes=["sb-accordion"]):
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
                            gdrive_remote = gr.Textbox(value=cfg["gdrive_remote"], label="Google Drive Remote")
                            gdrive_folder = gr.Textbox(value=cfg["gdrive_folder"], label="فولدر Drive")

                        save_btn = gr.Button("💾 حفظ جميع المفاتيح والإعدادات في النظام", variant="secondary")
                        save_status = gr.Markdown("")

                    with gr.Accordion("⚙️ التفريغ الصوتي (Whisper) والضغط (NVENC) وحقوق الفريق", open=False, elem_classes=["sb-accordion"]):
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
                            wrap_mode = gr.Dropdown(
                                choices=[
                                    ("تتابعي: السطر الطويل يصبح حدثين متتاليين زمنياً (أسلوب Celestial)", "sequential"),
                                    ("مكدس: سطران فوق بعض في نفس التوقيت", "stacked"),
                                    ("بدون تقسيم", "off"),
                                ],
                                value=cfg.get("wrap_mode", "sequential"),
                                label="تقسيم الأسطر العربية الطويلة",
                            )
                            soften_exclaim = gr.Checkbox(
                                value=bool(cfg.get("soften_exclaim", True)),
                                label="تخفيف علامات التعجب (تحويل ! الخبرية إلى نقطة وحذفها بعد أوه/آه، مع الإبقاء على الصياح)",
                            )
                        with gr.Row():
                            codec = gr.Dropdown(
                                choices=[
                                    ("HEVC كرت الشاشة (hevc_nvenc · نفس سرعة الكرت وحجم أصغر · موصى به)", "hevc_nvenc"),
                                    ("H.264 كرت الشاشة (h264_nvenc · توافق أوسع)", "h264_nvenc"),
                                    ("AV1 كرت الشاشة (av1_nvenc)", "av1_nvenc"),
                                    ("HEVC معالج x265 · أصغر حجم ممكن (أبطأ بكثير)", "libx265"),
                                ],
                                value=cfg["codec"],
                                label="الكوديك",
                            )
                            preset = gr.Dropdown(choices=["p7", "p6", "p5", "p4"], value=cfg["preset"], label="دقة الضغط")
                            cq = gr.Slider(minimum=18, maximum=30, step=1, value=cfg["cq"], label="الجودة CQ")
                            audio_bitrate = gr.Dropdown(choices=["128k", "160k", "192k"], value=cfg["audio_bitrate"], label="صوت AAC")
                        with gr.Row():
                            logo_position = gr.Dropdown(
                                choices=[("أعلى اليمين", "top-right"), ("أعلى الشمال", "top-left"), ("أسفل اليمين", "bottom-right"), ("أسفل الشمال", "bottom-left")],
                                value=cfg.get("logo_position", "top-right"),
                                label="🛡️ مكان اللوجو المحروق",
                            )
                            logo_opacity = gr.Slider(minimum=0.2, maximum=1.0, step=0.05, value=cfg.get("logo_opacity", 0.30), label="شفافية اللوجو")
                            logo_scale = gr.Slider(minimum=0.06, maximum=0.25, step=0.01, value=cfg.get("logo_scale", 0.12), label="حجم اللوجو (نسبة من عرض الفيديو)")
                        gr.Markdown("📎 حط ملف اللوجو في `assets/logo.png` جنب الكود — لو الملف مش موجود، الفيديو يطلع بدون لوجو عادي.")
                        with gr.Row():
                            credit_translator = gr.Textbox(value=cfg["credit_translator"], label="الترجمة")
                            credit_typesetter = gr.Textbox(value=cfg["credit_typesetter"], label="المحاكاة")
                            credit_encoder = gr.Textbox(value=cfg["credit_encoder"], label="الدمج والرفع")

                        cleanup_temp = gr.Checkbox(value=False, label="مسح الحلقة الخام بعد انتهاء الدمج (اتركه مغلقاً للاحتفاظ بالحلقات المحمّلة دائماً)")

                    step1_btn = gr.Button(
                        "⚔️ ARISE · ابدأ الطحن — ترجمة وحرق وضغط MP4 [STRIKE]",
                        variant="primary",
                        size="lg",
                        elem_id="sb-step1-btn",
                    )

                progress_hud = gr.HTML(value=render_progress_hud())

                with gr.Column(visible=False, elem_classes=["sb-wiz-step"]) as step2_group:
                    gr.HTML('<div style="font-weight:700;color:#ffb27f;border:1px solid rgba(255,178,127,.35);background:rgba(70,40,20,.55);padding:8px 14px;border-radius:10px;margin:10px 0;letter-spacing:.5px;font-size:15px;">🌀 الخطوة 2 · الحلقة جاهزة: اعتمد وارفع</div>')
                    with gr.Row():
                        enable_dood = gr.Checkbox(value=True, label="رفع على DoodStream")
                        enable_gdrive = gr.Checkbox(value=True, label="رفع على Google Drive")

                    step2_btn = gr.Button(
                        "🌀 الخطوة 2: عبور البوابة — اعتماد ورفع جميع الجودات [S-PORTAL]",
                        variant="stop",
                        size="lg",
                        elem_id="sb-step2-btn",
                    )

                    back_btn = gr.Button("↩️ رجوع — تجهيز حلقة جديدة", size="sm")

            with gr.Column(scale=1):
                preview_player = gr.Video(label="📺 مشغّل معاينة الحلقة بعد المحو والحرق")

                with gr.Row():
                    local_dropdown = gr.Dropdown(choices=list_local_outputs(), label="📂 تصفّح الحلقات المحروقة الجاهزة (/workspace/outputs)", interactive=True)
                    refresh_local_btn = gr.Button("🔄 تحديث", size="sm")

                links_box = gr.Textbox(label="🔗 روابط الرفع الجاهزة للنشر", lines=8, interactive=False, visible=False)
                log_box = gr.Textbox(label="📋 سجل عمليات النظام المباشر [HUNTER LOG]", lines=16, max_lines=22, interactive=False)
                with gr.Group(visible=False) as downloads_group:
                    out_video_files = gr.Files(label="🎥 تحميل الفيديوهات النهائية لجهازك")
                    out_ass_file = gr.File(label="📝 تحميل ملف الترجمة المنسّق (.ass)")

        provider.change(fn=_on_provider_change, inputs=[provider], outputs=[llm_model, base_url])

        series_picker.change(
            fn=lambda pick: gr.update(visible=(pick == SERIES_NEW_OPT), value=""),
            inputs=[series_picker],
            outputs=[series_name],
        )

        save_btn.click(
            fn=save_settings,
            inputs=[
                provider, llm_model, api_key, base_url, style_preset, wrap_mode, soften_exclaim, dood_api_key,
                gdrive_remote, gdrive_folder,
                codec, preset, cq, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
                logo_position, logo_opacity, logo_scale,
            ],
            outputs=[save_status],
        )

        refresh_raw_btn.click(fn=lambda: gr.update(choices=[""] + list_cached_raw_videos()), outputs=[saved_raw_1080])
        refresh_local_btn.click(fn=lambda: gr.update(choices=list_local_outputs()), outputs=[local_dropdown])
        local_dropdown.change(fn=_browser_preview_path, inputs=[local_dropdown], outputs=[preview_player])

        step1_btn.click(
            fn=step1_process_for_preview,
            inputs=[
                episode_name, series_name, series_picker, style_preset, wrap_mode, soften_exclaim,
                saved_raw_1080,
                link_1080, file_1080,
                link_720, file_720,
                link_480, file_480,
                sub_mode, whisper_model,
                provider, api_key, llm_model, base_url,
                codec, cq, preset, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
                cleanup_temp,
                logo_position, logo_opacity, logo_scale,
            ],
            outputs=[
                progress_hud, log_box, preview_player, local_dropdown, saved_raw_1080,
                out_video_files, out_ass_file, job_state,
                step1_group, step2_group, downloads_group, links_box,
            ],
        )

        batch_btn.click(
            fn=step_batch_process,
            inputs=[
                batch_links, series_name, series_picker, style_preset, wrap_mode, soften_exclaim,
                sub_mode, whisper_model,
                provider, api_key, llm_model, base_url,
                codec, cq, preset, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
                cleanup_temp,
                logo_position, logo_opacity, logo_scale,
            ],
            outputs=[
                progress_hud, log_box, preview_player, local_dropdown, saved_raw_1080,
                out_video_files, out_ass_file, job_state,
                step1_group, step2_group, downloads_group, links_box,
            ],
        )

        step2_btn.click(
            fn=step2_approve_and_upload,
            inputs=[
                job_state,
                enable_dood, dood_api_key,
                enable_gdrive, gdrive_remote, gdrive_folder,
            ],
            outputs=[progress_hud, log_box, links_box],
        )

        back_btn.click(
            fn=lambda: (
                gr.update(visible=True),
                gr.update(visible=False),
                gr.update(visible=False),
                gr.update(value="", visible=False),
            ),
            outputs=[step1_group, step2_group, downloads_group, links_box],
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
    if "allowed_paths" in launch_sig:
        launch_kwargs["allowed_paths"] = [str(STORAGE_ROOT), str(RAW_CACHE_ROOT)]
    if "delete_cache" in launch_sig:
        launch_kwargs["delete_cache"] = (86400, 86400)

    ui.queue().launch(**launch_kwargs)

