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
from typing import Callable, Dict, List, Optional, Tuple

import gradio as gr

from media_ops import (
    VIDEO_EXTENSIONS,
    burn_and_compress_native,
    download_torrent_or_url,
    upload_to_doodstream,
    upload_to_rclone,
)
from subtitles_ai import (
    extract_embedded_subtitle,
    parse_source_subtitles,
    probe_embedded_subtitles,
    transcribe_with_whisper,
    translate_entries,
    write_styled_ass,
)

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"
STORAGE_ROOT = Path(os.environ.get("RUNPOD_OUTPUT_ROOT", "/workspace/outputs"))
RAW_CACHE_ROOT = Path(os.environ.get("RUNPOD_RAW_CACHE", "/workspace/raw_episodes"))
LEGACY_TEMP_ROOT = Path(os.environ.get("RUNPOD_TEMP_ROOT", "/workspace/tmp_downloads"))
SETTINGS_FILE = Path(os.environ.get("RUNPOD_SETTINGS_FILE", "/workspace/subber_settings.json"))


# ==============================================================================
# SHADOW BLADE THEME (CSS + 60FPS CANVAS VFX ENGINE + LIVE PROGRESS HUD)
# ==============================================================================

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
    --sb-cyan-dim: rgba(56, 189, 248, 0.22);
    --sb-violet: #A78BFA;
    --sb-violet-dim: rgba(167, 139, 250, 0.24);
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
    max-width: 1480px !important;
    margin: 0 auto !important;
    padding: 16px 20px 36px !important;
}

body.sb-screen-shake .gradio-container {
    animation: sb-shake-anim 0.42s cubic-bezier(.36,.07,.19,.97) both;
}

@keyframes sb-shake-anim {
    0%, 100% { transform: translate3d(0, 0, 0); }
    15% { transform: translate3d(-5px, -3px, 0) rotate(-0.3deg); }
    30% { transform: translate3d(5px, 3px, 0) rotate(0.3deg); }
    45% { transform: translate3d(-4px, 2px, 0); }
    60% { transform: translate3d(4px, -2px, 0); }
    75% { transform: translate3d(-2px, 1px, 0); }
}

#sb-global-vfx-canvas {
    position: fixed;
    inset: 0;
    width: 100vw;
    height: 100vh;
    pointer-events: none;
    z-index: 9998;
}

#sb-hud-toast {
    position: fixed;
    top: 22px;
    left: 50%;
    transform: translateX(-50%) translateY(-130%);
    z-index: 9999;
    min-width: 340px;
    max-width: 92vw;
    background: rgba(8, 14, 30, 0.94);
    border: 1px solid var(--sb-cyan);
    box-shadow: 0 0 28px rgba(56, 189, 248, 0.35), inset 0 0 18px rgba(56, 189, 248, 0.12);
    padding: 12px 22px;
    border-radius: 4px;
    pointer-events: none;
    transition: transform 0.34s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.28s ease;
    opacity: 0;
    direction: rtl;
    text-align: right;
    backdrop-filter: blur(10px);
}

#sb-hud-toast.sb-visible {
    transform: translateX(-50%) translateY(0);
    opacity: 1;
}

#sb-hud-toast.sb-violet-mode {
    border-color: var(--sb-violet);
    box-shadow: 0 0 28px rgba(167, 139, 250, 0.4), inset 0 0 18px rgba(167, 139, 250, 0.14);
}

.sb-toast-tag {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--sb-cyan);
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 3px;
}

#sb-hud-toast.sb-violet-mode .sb-toast-tag {
    color: var(--sb-violet);
}

.sb-toast-msg {
    font-size: 13.5px;
    font-weight: 700;
    color: var(--sb-text);
}

.sb-hero-wrap {
    position: relative;
    background: linear-gradient(135deg, rgba(11, 19, 38, 0.96) 0%, rgba(7, 12, 26, 0.98) 100%);
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-right: 4px solid var(--sb-cyan);
    border-radius: 6px;
    padding: 18px 22px;
    margin-bottom: 18px;
    overflow: hidden;
    box-shadow: 0 10px 32px rgba(0, 0, 0, 0.55), inset 0 0 24px rgba(56, 189, 248, 0.05);
}

.sb-hero-wrap::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    width: 180px;
    height: 2px;
    background: linear-gradient(90deg, var(--sb-violet), transparent);
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
    position: relative;
    width: 64px;
    height: 64px;
    background: #060B18;
    border: 1px solid rgba(56, 189, 248, 0.45);
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    flex-shrink: 0;
    box-shadow: 0 0 18px rgba(56, 189, 248, 0.2);
    transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}

.sb-emblem-box:hover {
    transform: scale(1.06) rotate(-2deg);
    border-color: var(--sb-cyan-bright);
    box-shadow: 0 0 26px rgba(56, 189, 248, 0.45);
}

.sb-title-eyebrow {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.16em;
    color: var(--sb-cyan);
    text-transform: uppercase;
    margin-bottom: 2px;
}

.sb-main-title {
    font-size: 23px;
    font-weight: 900;
    color: #F8FAFC;
    margin: 0;
    line-height: 1.25;
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
    margin: 5px 0 0;
    font-size: 13.5px;
    color: var(--sb-muted);
    font-weight: 600;
}

.sb-subtitle-desc strong {
    color: var(--sb-cyan-bright);
}

.sb-hud-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
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

#sb-test-slash-trigger {
    font-family: 'Cairo', sans-serif;
    font-size: 12.5px;
    font-weight: 800;
    color: #040710;
    background: var(--sb-cyan);
    border: none;
    padding: 7px 14px;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.18s ease;
    box-shadow: 0 0 14px rgba(56, 189, 248, 0.35);
}

#sb-test-slash-trigger:hover {
    background: var(--sb-cyan-bright);
    transform: translateY(-1px);
    box-shadow: 0 0 22px rgba(56, 189, 248, 0.6);
}

.sb-combat-stage-card {
    background: var(--sb-panel);
    border: 1px solid rgba(56, 189, 248, 0.24);
    border-radius: 6px;
    overflow: hidden;
    margin-bottom: 12px;
}

.sb-stage-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 12px;
    background: rgba(4, 7, 16, 0.8);
    border-bottom: 1px solid rgba(56, 189, 248, 0.16);
    font-size: 12px;
    color: var(--sb-muted);
    font-weight: 700;
}

.sb-stage-topbar span.sb-mono {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10.5px;
    color: var(--sb-cyan);
}

#sb-anime-preview-canvas {
    display: block;
    width: 100%;
    height: 150px;
    cursor: pointer;
    background: #040710;
}

.gradio-container .block,
.gradio-container .form,
.gradio-container .gr-box,
.gradio-container .gr-panel {
    background-color: var(--sb-panel) !important;
    border-color: rgba(56, 189, 248, 0.18) !important;
    border-radius: 6px !important;
}

.gradio-container .accordion,
.sb-accordion {
    background: var(--sb-panel) !important;
    border: 1px solid rgba(56, 189, 248, 0.22) !important;
    border-radius: 6px !important;
    margin-bottom: 12px !important;
    overflow: hidden !important;
}

.gradio-container .label-wrap,
.gradio-container .accordion > button {
    background: linear-gradient(90deg, rgba(15, 26, 51, 0.9), rgba(11, 19, 38, 0.95)) !important;
    color: var(--sb-text) !important;
    font-weight: 800 !important;
    font-size: 14px !important;
    padding: 11px 14px !important;
    direction: rtl !important;
}

.gradio-container label span,
.gradio-container .block label,
.gradio-container legend span {
    color: #E2E8F0 !important;
    font-family: 'Cairo', sans-serif !important;
    font-weight: 700 !important;
    font-size: 13px !important;
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
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}

.gradio-container input:focus,
.gradio-container textarea:focus {
    border-color: var(--sb-cyan) !important;
    box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.18) !important;
    outline: none !important;
}

#sb-log-box textarea {
    background-color: #03060E !important;
    color: #7DD3FC !important;
    font-family: 'JetBrains Mono', 'Cairo', monospace !important;
    font-size: 12.5px !important;
    line-height: 1.65 !important;
    border: 1px solid rgba(56, 189, 248, 0.28) !important;
    direction: rtl !important;
}

#sb-links-box textarea {
    background-color: #050914 !important;
    color: #C4B5FD !important;
    font-family: 'JetBrains Mono', 'Cairo', monospace !important;
    font-size: 12.5px !important;
    border: 1px solid rgba(167, 139, 250, 0.32) !important;
    unicode-bidi: plaintext !important;
}

#sb-step1-btn,
#sb-step2-btn {
    position: relative !important;
    overflow: hidden !important;
    min-height: 68px !important;
    border-radius: 6px !important;
    font-family: 'Cairo', sans-serif !important;
    font-size: 17px !important;
    font-weight: 900 !important;
    letter-spacing: 0.02em !important;
    cursor: pointer !important;
    transition: transform 0.18s ease, box-shadow 0.22s ease, border-color 0.22s ease !important;
    z-index: 1 !important;
    margin: 10px 0 !important;
}

#sb-step1-btn {
    background: #061024 !important;
    color: #F8FAFC !important;
    border: 1.5px solid var(--sb-cyan) !important;
    box-shadow: 0 0 24px rgba(56, 189, 248, 0.25), inset 0 0 22px rgba(56, 189, 248, 0.14) !important;
    text-shadow: 0 2px 10px rgba(4, 7, 16, 0.95), 0 0 14px rgba(56, 189, 248, 0.65) !important;
}

#sb-step1-btn:hover {
    transform: translateY(-2px) !important;
    border-color: #BAE6FD !important;
    box-shadow: 0 0 36px rgba(56, 189, 248, 0.48), inset 0 0 28px rgba(56, 189, 248, 0.24) !important;
}

#sb-step2-btn {
    background: #0D0924 !important;
    color: #F8FAFC !important;
    border: 1.5px solid var(--sb-violet) !important;
    box-shadow: 0 0 24px rgba(167, 139, 250, 0.24), inset 0 0 22px rgba(167, 139, 250, 0.15) !important;
    text-shadow: 0 2px 10px rgba(4, 7, 16, 0.95), 0 0 14px rgba(167, 139, 250, 0.65) !important;
}

#sb-step2-btn:hover {
    transform: translateY(-2px) !important;
    border-color: #DDD6FE !important;
    box-shadow: 0 0 36px rgba(167, 139, 250, 0.48), inset 0 0 28px rgba(167, 139, 250, 0.25) !important;
}

#sb-save-btn {
    background: rgba(15, 26, 51, 0.95) !important;
    color: var(--sb-cyan-bright) !important;
    border: 1px solid rgba(56, 189, 248, 0.42) !important;
    font-family: 'Cairo', sans-serif !important;
    font-weight: 800 !important;
    border-radius: 5px !important;
    min-height: 44px !important;
    transition: all 0.18s ease !important;
}

#sb-save-btn:hover {
    background: rgba(56, 189, 248, 0.14) !important;
    border-color: var(--sb-cyan) !important;
    box-shadow: 0 0 18px rgba(56, 189, 248, 0.25) !important;
}

#sb-refresh-btn {
    background: rgba(15, 26, 51, 0.9) !important;
    color: var(--sb-text) !important;
    border: 1px solid rgba(56, 189, 248, 0.3) !important;
    font-family: 'Cairo', sans-serif !important;
    font-weight: 700 !important;
}

.sb-btn-vfx-canvas {
    position: absolute !important;
    inset: 0 !important;
    width: 100% !important;
    height: 100% !important;
    pointer-events: none !important;
    z-index: 0 !important;
}

#sb-step1-btn > *,
#sb-step2-btn > * {
    position: relative;
    z-index: 2;
}

.gradio-container input[type="checkbox"],
.gradio-container input[type="radio"],
.gradio-container input[type="range"] {
    accent-color: var(--sb-cyan) !important;
}

#sb-video-player {
    border: 1px solid rgba(56, 189, 248, 0.3) !important;
    border-radius: 6px !important;
    background: #03060E !important;
    overflow: hidden !important;
}

/* Native Shadow Blade Live Progress HUD */
.sb-prog-card {
    background: linear-gradient(135deg, rgba(11, 19, 38, 0.98) 0%, rgba(5, 10, 22, 0.99) 100%);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-right: 4px solid var(--sb-cyan);
    border-radius: 6px;
    padding: 14px 18px;
    margin: 10px 0 12px;
    box-shadow: 0 8px 26px rgba(0, 0, 0, 0.5), inset 0 0 20px rgba(56, 189, 248, 0.06);
    direction: rtl;
}
.sb-prog-card.sb-mode-violet {
    border-color: rgba(167, 139, 250, 0.42);
    border-right-color: var(--sb-violet);
}
.sb-prog-card.sb-mode-done {
    border-color: rgba(52, 211, 153, 0.45);
    border-right-color: var(--sb-success);
}
.sb-prog-card.sb-mode-error {
    border-color: rgba(251, 113, 133, 0.5);
    border-right-color: var(--sb-danger);
}
.sb-prog-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;
}
.sb-prog-badge {
    font-family: 'JetBrains Mono', 'Cairo', monospace;
    font-size: 11.5px;
    font-weight: 800;
    color: var(--sb-cyan-bright);
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.32);
    padding: 2px 9px;
    border-radius: 4px;
    display: inline-block;
    margin-bottom: 4px;
}
.sb-mode-violet .sb-prog-badge {
    color: #DDD6FE;
    background: rgba(167, 139, 250, 0.14);
    border-color: rgba(167, 139, 250, 0.38);
}
.sb-mode-done .sb-prog-badge {
    color: #6EE7B7;
    background: rgba(52, 211, 153, 0.14);
    border-color: rgba(52, 211, 153, 0.4);
}
.sb-mode-error .sb-prog-badge {
    color: #FDA4AF;
    background: rgba(251, 113, 133, 0.14);
    border-color: rgba(251, 113, 133, 0.4);
}
.sb-prog-title {
    font-size: 14.5px;
    font-weight: 800;
    color: #F8FAFC;
}
.sb-prog-pct {
    font-family: 'JetBrains Mono', monospace;
    font-size: 28px;
    font-weight: 900;
    color: var(--sb-cyan-bright);
    text-shadow: 0 0 16px rgba(56, 189, 248, 0.55);
    line-height: 1;
    direction: ltr;
}
.sb-mode-violet .sb-prog-pct {
    color: #DDD6FE;
    text-shadow: 0 0 16px rgba(167, 139, 250, 0.55);
}
.sb-mode-done .sb-prog-pct {
    color: var(--sb-success);
    text-shadow: 0 0 16px rgba(52, 211, 153, 0.5);
}
.sb-mode-error .sb-prog-pct {
    color: var(--sb-danger);
}
.sb-prog-track {
    width: 100%;
    height: 12px;
    background: #03060E;
    border: 1px solid rgba(56, 189, 248, 0.25);
    border-radius: 999px;
    overflow: hidden;
    position: relative;
}
.sb-prog-fill {
    height: 100%;
    background: linear-gradient(90deg, #38BDF8 0%, #818CF8 60%, #A78BFA 100%);
    box-shadow: 0 0 14px rgba(56, 189, 248, 0.7);
    border-radius: 999px;
    transition: width 0.3s ease;
}
.sb-mode-done .sb-prog-fill {
    background: linear-gradient(90deg, #10B981 0%, #34D399 100%);
    box-shadow: 0 0 14px rgba(52, 211, 153, 0.65);
}
.sb-mode-error .sb-prog-fill {
    background: var(--sb-danger);
}
.sb-prog-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-top: 8px;
    font-size: 12px;
    color: var(--sb-muted);
    font-family: 'JetBrains Mono', 'Cairo', monospace;
    flex-wrap: wrap;
}
"""

SHADOW_BLADE_HERO_HTML = """
<div id="sb-hud-toast">
    <div class="sb-toast-tag">
        <span id="sb-toast-code">[HUNTER HUD · S-RANK]</span>
        <span>⚡ ACTIVE</span>
    </div>
    <div class="sb-toast-msg" id="sb-toast-text">تم تفعيل محرك نصل الظل — جاهز لبدء المهمة!</div>
</div>

<div class="sb-hero-wrap" id="sb-hero-header">
    <div class="sb-hero-inner">
        <div class="sb-brand-side">
            <div class="sb-emblem-box" id="sb-logo-trigger" title="اضغط لتجربة ضربة النصل القاطعة">
                <svg viewBox="0 0 64 64" width="46" height="46" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <polygon points="32,4 58,18 58,46 32,60 6,46 6,18" stroke="#38BDF8" stroke-width="1.6" fill="#070D1D"/>
                    <path d="M16 48 L47 15 L51 19 L22 50 Z" fill="#38BDF8"/>
                    <path d="M48 48 L17 15 L13 19 L42 50 Z" fill="#A78BFA" fill-opacity="0.9"/>
                    <circle cx="32" cy="32" r="4.5" fill="#F8FAFC"/>
                </svg>
            </div>
            <div>
                <div class="sb-title-eyebrow">SHADOW BLADE · ANIME SUBBER PIPELINE</div>
                <h1 class="sb-main-title">
                    نصل الظل · سَبّر الأنمي
                    <span class="sb-rank-pill">HUNTER HUD · S-RANK</span>
                </h1>
                <p class="sb-subtitle-desc">
                    <strong>المرحلة 1 [STRIKE]:</strong> تحميل التورنت (حفظ دائم) والترجمة بالذكاء الاصطناعي والدمج بـ NVENC للمعاينة
                    &nbsp;←&nbsp;
                    <strong>المرحلة 2 [S-PORTAL]:</strong> اعتماد ورفع جميع الجودات واستخراج الروابط
                </p>
            </div>
        </div>
        <div class="sb-hud-actions">
            <div class="sb-hud-chip">
                <span class="sb-dot-online"></span>
                <span>NVENC · WHISPER · GEMINI</span>
            </div>
            <button type="button" id="sb-test-slash-trigger">⚡ اختبر ضربة النصل</button>
        </div>
    </div>
</div>
"""

SHADOW_BLADE_STAGE_HTML = """
<div class="sb-combat-stage-card">
    <div class="sb-stage-topbar">
        <span>⚔️ معاينة حية لستايل حرق الترجمة (اضغط داخل المشهد لتبديل الجملة وضربة النصل)</span>
        <span class="sb-mono">60FPS HUNTER VFX</span>
    </div>
    <canvas id="sb-anime-preview-canvas"></canvas>
</div>
"""

SHADOW_BLADE_JS = r"""
() => {
    if (window.__SHADOW_BLADE_BOOTED__) return;
    window.__SHADOW_BLADE_BOOTED__ = true;

    const getRoot = () => {
        const app = document.querySelector("gradio-app");
        return (app && app.shadowRoot) ? app.shadowRoot : document;
    };

    const ACTION_SUB_LINES = [
        "حسمتُ أمري... سأتجاوز هذا الاختبار بنفسي الآن!",
        "سرعة مذهلة! بالكاد تمكنتُ من رصد حركة النصل!",
        "جميع الأنظمة جاهزة — لنبدأ الهجوم الحاسم بأقصى سرعة!",
        "تركيز كامل... ضربة واحدة متقنة ستكفي لحسم المواجهة!"
    ];
    let currentSubIdx = 0;
    let stageFlash = 0;

    let globalCanvas = document.getElementById("sb-global-vfx-canvas");
    if (!globalCanvas) {
        globalCanvas = document.createElement("canvas");
        globalCanvas.id = "sb-global-vfx-canvas";
        document.body.appendChild(globalCanvas);
    }
    const gCtx = globalCanvas.getContext("2d");

    const resizeGlobal = () => {
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        globalCanvas.width = Math.floor(window.innerWidth * dpr);
        globalCanvas.height = Math.floor(window.innerHeight * dpr);
        gCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    window.addEventListener("resize", resizeGlobal);
    resizeGlobal();

    const slashes = [];
    const shockwaves = [];
    const sparks = [];
    const floatingTags = [];
    let toastTimer = null;

    const showHudToast = (code, msg, violet = false) => {
        const root = getRoot();
        const toast = root.getElementById ? root.getElementById("sb-hud-toast") : document.getElementById("sb-hud-toast");
        const codeEl = root.getElementById ? root.getElementById("sb-toast-code") : document.getElementById("sb-toast-code");
        const txtEl = root.getElementById ? root.getElementById("sb-toast-text") : document.getElementById("sb-toast-text");
        if (!toast || !codeEl || !txtEl) return;

        codeEl.textContent = code;
        txtEl.textContent = msg;
        toast.classList.toggle("sb-violet-mode", !!violet);
        toast.classList.add("sb-visible");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toast.classList.remove("sb-visible"), 3400);
    };

    const triggerScreenShake = () => {
        document.body.classList.remove("sb-screen-shake");
        void document.body.offsetWidth;
        document.body.classList.add("sb-screen-shake");
        setTimeout(() => document.body.classList.remove("sb-screen-shake"), 450);
    };

    const fireTwinDaggerStrike = (originX, originY, mode = "strike") => {
        const cx = originX || window.innerWidth * 0.5;
        const cy = originY || window.innerHeight * 0.5;
        const isPortal = mode === "portal";
        const mainColor = isPortal ? "#A78BFA" : "#38BDF8";
        const subColor = isPortal ? "#38BDF8" : "#A78BFA";

        triggerScreenShake();
        stageFlash = 1.0;

        slashes.push({
            x1: cx - 360, y1: cy + 170,
            x2: cx + 360, y2: cy - 170,
            life: 1.0, color: mainColor, width: 7
        });
        slashes.push({
            x1: cx - 310, y1: cy - 155,
            x2: cx + 310, y2: cy + 155,
            life: 1.0, color: subColor, width: 5.5
        });

        shockwaves.push({ x: cx, y: cy, r: 12, maxR: 340, life: 1.0, color: mainColor });
        shockwaves.push({ x: cx, y: cy, r: 4, maxR: 210, life: 0.9, color: "#F8FAFC" });

        for (let i = 0; i < 42; i++) {
            const angle = (Math.PI * 2 * i) / 42 + (Math.random() - 0.5) * 0.25;
            const speed = 4 + Math.random() * 11;
            sparks.push({
                x: cx,
                y: cy,
                vx: Math.cos(angle) * speed,
                vy: Math.sin(angle) * speed,
                len: 8 + Math.random() * 16,
                life: 0.75 + Math.random() * 0.35,
                color: i % 3 === 0 ? "#F8FAFC" : (i % 2 === 0 ? mainColor : subColor)
            });
        }

        const words = isPortal ? ["S-PORTAL", "UPLOADING", "LINK SYNC"] : ["STRIKE!", "S-RANK", "NVENC ⚡"];
        words.forEach((w, idx) => {
            floatingTags.push({
                text: w,
                x: cx + (idx - 1) * 115,
                y: cy - 24 - idx * 12,
                vy: -1.8 - Math.random() * 1.2,
                life: 1.0,
                color: idx % 2 === 0 ? mainColor : "#F8FAFC"
            });
        });
    };

    const attachButtonCanvas = (btn, type) => {
        if (!btn || btn.dataset.sbVfxBound === "1") return;
        btn.dataset.sbVfxBound = "1";

        const cvs = document.createElement("canvas");
        cvs.className = "sb-btn-vfx-canvas";
        btn.insertBefore(cvs, btn.firstChild);

        let hovered = false;
        btn.addEventListener("mouseenter", () => { hovered = true; });
        btn.addEventListener("mouseleave", () => { hovered = false; });
        btn.addEventListener("click", (ev) => {
            const rect = btn.getBoundingClientRect();
            const x = ev.clientX || (rect.left + rect.width / 2);
            const y = ev.clientY || (rect.top + rect.height / 2);
            if (type === "strike") {
                fireTwinDaggerStrike(x, y, "strike");
                showHudToast(
                    "[HUNTER SYSTEM · STRIKE]",
                    "انطلاق المرحلة 1: جاري التحميل والترجمة والضغط للمعاينة!",
                    false
                );
            } else {
                fireTwinDaggerStrike(x, y, "portal");
                showHudToast(
                    "[S-PORTAL · CLOUD SYNC]",
                    "عبور البوابة السحابية: جاري رفع الجودات واستخراج الروابط!",
                    true
                );
            }
        });

        const particles = Array.from({ length: 22 }, () => ({
            x: Math.random(),
            y: Math.random(),
            s: 1 + Math.random() * 2.2,
            v: 0.003 + Math.random() * 0.008,
            a: Math.random() * Math.PI * 2
        }));

        btn.__sbRender = (t) => {
            const rect = btn.getBoundingClientRect();
            if (rect.width < 10 || rect.height < 10) return;
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const targetW = Math.floor(rect.width * dpr);
            const targetH = Math.floor(rect.height * dpr);
            if (cvs.width !== targetW || cvs.height !== targetH) {
                cvs.width = targetW;
                cvs.height = targetH;
            }
            const ctx = cvs.getContext("2d");
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const w = rect.width;
            const h = rect.height;
            ctx.clearRect(0, 0, w, h);

            const speedMul = hovered ? 2.4 : 1.0;

            if (type === "strike") {
                const eyeBaseX = 46;
                const eyeBaseY = h * 0.5;
                const pulse = 0.75 + 0.25 * Math.sin(t * 0.005 * speedMul);

                for (let p of particles) {
                    p.y -= p.v * speedMul;
                    if (p.y < -0.1) { p.y = 1.1; p.x = Math.random(); }
                    ctx.fillStyle = `rgba(56, 189, 248, ${hovered ? 0.26 : 0.14})`;
                    ctx.beginPath();
                    ctx.arc(p.x * w, p.y * h, p.s, 0, Math.PI * 2);
                    ctx.fill();
                }

                const drawEye = (ex, ey, tilt) => {
                    ctx.save();
                    ctx.translate(ex, ey);
                    ctx.rotate(tilt);
                    ctx.shadowColor = "#38BDF8";
                    ctx.shadowBlur = hovered ? 18 : 11;
                    ctx.fillStyle = `rgba(56, 189, 248, ${0.85 * pulse})`;
                    ctx.beginPath();
                    ctx.moveTo(-9, 0);
                    ctx.quadraticCurveTo(0, -4.5, 9, -1);
                    ctx.quadraticCurveTo(0, 3.8, -9, 0);
                    ctx.fill();

                    ctx.fillStyle = "#F8FAFC";
                    ctx.beginPath();
                    ctx.arc(0, -0.3, 2.1, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.restore();
                };
                drawEye(eyeBaseX - 12, eyeBaseY, 0.14);
                drawEye(eyeBaseX + 12, eyeBaseY, -0.14);

                if (Math.random() < (hovered ? 0.55 : 0.22)) {
                    ctx.save();
                    ctx.strokeStyle = hovered ? "#BAE6FD" : "rgba(56, 189, 248, 0.55)";
                    ctx.lineWidth = hovered ? 1.8 : 1.1;
                    ctx.shadowColor = "#38BDF8";
                    ctx.shadowBlur = 10;
                    ctx.beginPath();
                    let lx = 0;
                    let ly = h * (0.25 + Math.random() * 0.5);
                    ctx.moveTo(lx, ly);
                    const steps = 9;
                    for (let i = 1; i <= steps; i++) {
                        lx = (w / steps) * i;
                        ly += (Math.random() - 0.5) * 20;
                        ctx.lineTo(lx, Math.max(4, Math.min(h - 4, ly)));
                    }
                    ctx.stroke();
                    ctx.restore();
                }
            } else {
                const px = 48;
                const py = h * 0.5;
                const rot = t * 0.0024 * speedMul;

                ctx.save();
                ctx.translate(px, py);
                ctx.shadowColor = "#A78BFA";
                ctx.shadowBlur = hovered ? 16 : 9;

                ctx.rotate(rot);
                ctx.strokeStyle = "rgba(167, 139, 250, 0.75)";
                ctx.lineWidth = 1.8;
                ctx.setLineDash([7, 5]);
                ctx.beginPath();
                ctx.arc(0, 0, 21, 0, Math.PI * 2);
                ctx.stroke();

                ctx.rotate(-rot * 2.1);
                ctx.setLineDash([]);
                ctx.strokeStyle = "rgba(56, 189, 248, 0.85)";
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                for (let k = 0; k < 6; k++) {
                    const ang = (Math.PI / 3) * k;
                    const rx = Math.cos(ang) * 12;
                    const ry = Math.sin(ang) * 12;
                    if (k === 0) ctx.moveTo(rx, ry);
                    else ctx.lineTo(rx, ry);
                }
                ctx.closePath();
                ctx.stroke();

                ctx.fillStyle = "#DDD6FE";
                ctx.beginPath();
                ctx.arc(0, 0, hovered ? 4.2 : 3, 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();

                for (let p of particles) {
                    p.a += 0.03 * speedMul;
                    p.x -= p.v * speedMul;
                    if (p.x < 0) p.x = 1;
                    const yWave = (p.y * h) + Math.sin(p.a + p.x * 6) * 5;
                    ctx.fillStyle = p.s > 2 ? "rgba(167, 139, 250, 0.35)" : "rgba(56, 189, 248, 0.28)";
                    ctx.beginPath();
                    ctx.arc(p.x * w, yWave, p.s * 0.9, 0, Math.PI * 2);
                    ctx.fill();
                }
            }
        };
    };

    const renderStageCanvas = (stageCvs, t) => {
        if (!stageCvs) return;
        const rect = stageCvs.getBoundingClientRect();
        if (rect.width < 20) return;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const tw = Math.floor(rect.width * dpr);
        const th = Math.floor(rect.height * dpr);
        if (stageCvs.width !== tw || stageCvs.height !== th) {
            stageCvs.width = tw;
            stageCvs.height = th;
        }
        const ctx = stageCvs.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const w = rect.width;
        const h = rect.height;

        ctx.fillStyle = "#040710";
        ctx.fillRect(0, 0, w, h);

        ctx.strokeStyle = "rgba(56, 189, 248, 0.09)";
        ctx.lineWidth = 1;
        for (let i = 0; i < 12; i++) {
            const y = ((i * 17 + t * 0.04) % h);
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(w * 0.45, y - 6);
            ctx.stroke();
        }

        const hx = w * 0.18;
        const hy = h * 0.56;
        const cloakWave = Math.sin(t * 0.004) * 7;

        ctx.save();
        ctx.fillStyle = "#0B152B";
        ctx.strokeStyle = "rgba(56, 189, 248, 0.35)";
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.moveTo(hx - 24, hy + 45);
        ctx.quadraticCurveTo(hx - 42 + cloakWave, hy + 8, hx - 10, hy - 22);
        ctx.lineTo(hx + 14, hy - 22);
        ctx.quadraticCurveTo(hx + 34, hy + 10, hx + 28, hy + 45);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

        ctx.shadowColor = "#38BDF8";
        ctx.shadowBlur = 14;
        ctx.strokeStyle = "#38BDF8";
        ctx.lineWidth = 2.4;
        ctx.beginPath();
        ctx.moveTo(hx + 4, hy - 14);
        ctx.quadraticCurveTo(hx + 24, hy - 22, hx + 46 + cloakWave * 0.8, hy - 18 + Math.cos(t * 0.006) * 4);
        ctx.stroke();

        ctx.fillStyle = "#F8FAFC";
        ctx.beginPath();
        ctx.arc(hx + 4, hy - 14, 2.3, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        ctx.save();
        ctx.strokeStyle = "rgba(167, 139, 250, 0.35)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(hx + 16, hy + 14);
        ctx.lineTo(hx + 66, hy - 24);
        ctx.stroke();
        ctx.restore();

        if (stageFlash > 0.01) {
            ctx.fillStyle = `rgba(56, 189, 248, ${stageFlash * 0.28})`;
            ctx.fillRect(0, 0, w, h);
            stageFlash *= 0.88;
        }

        const subText = ACTION_SUB_LINES[currentSubIdx];
        ctx.save();
        ctx.font = "800 16px 'Cairo', sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.lineJoin = "round";
        ctx.strokeStyle = "#02040A";
        ctx.lineWidth = 4.5;
        ctx.shadowColor = "rgba(0, 0, 0, 0.9)";
        ctx.shadowBlur = 8;
        ctx.strokeText(subText, w * 0.54, h - 16);
        ctx.fillStyle = "#F8FAFC";
        ctx.fillText(subText, w * 0.54, h - 16);
        ctx.restore();
    };

    const bindDomElements = () => {
        const root = getRoot();
        const q = (sel) => (root.querySelector ? root.querySelector(sel) : null) || document.querySelector(sel);

        const step1Btn = q("#sb-step1-btn");
        const step2Btn = q("#sb-step2-btn");
        if (step1Btn) attachButtonCanvas(step1Btn, "strike");
        if (step2Btn) attachButtonCanvas(step2Btn, "portal");

        const saveBtn = q("#sb-save-btn");
        if (saveBtn && saveBtn.dataset.sbToastBound !== "1") {
            saveBtn.dataset.sbToastBound = "1";
            saveBtn.addEventListener("click", () => {
                showHudToast("[HUNTER CONFIG · SAVED]", "تم حفظ المفاتيح والإعدادات الدائمة في النظام بنجاح!", false);
            });
        }

        const testBtn = q("#sb-test-slash-trigger");
        if (testBtn && testBtn.dataset.sbBound !== "1") {
            testBtn.dataset.sbBound = "1";
            testBtn.addEventListener("click", (e) => {
                const r = testBtn.getBoundingClientRect();
                currentSubIdx = (currentSubIdx + 1) % ACTION_SUB_LINES.length;
                fireTwinDaggerStrike(r.left + r.width / 2, r.top + 120, "strike");
                showHudToast("[TWIN BLADE · VFX TEST]", "تم اختبار محرك ضربة النصل وتحديث جملة المعاينة!", false);
            });
        }

        const logoBox = q("#sb-logo-trigger");
        if (logoBox && logoBox.dataset.sbBound !== "1") {
            logoBox.dataset.sbBound = "1";
            logoBox.addEventListener("click", () => {
                const r = logoBox.getBoundingClientRect();
                fireTwinDaggerStrike(r.left + r.width / 2, r.top + r.height / 2, "portal");
                showHudToast("[SHADOW BLADE · READY]", "نظام سَبّر الأنمي في وضع الاستعداد القصوى!", true);
            });
        }

        const stageCvs = q("#sb-anime-preview-canvas");
        if (stageCvs && stageCvs.dataset.sbBound !== "1") {
            stageCvs.dataset.sbBound = "1";
            stageCvs.addEventListener("click", (ev) => {
                currentSubIdx = (currentSubIdx + 1) % ACTION_SUB_LINES.length;
                fireTwinDaggerStrike(ev.clientX, ev.clientY, "strike");
            });
        }
    };

    const observer = new MutationObserver(() => bindDomElements());
    observer.observe(document.body, { childList: true, subtree: true });
    bindDomElements();

    const tick = (t) => {
        const root = getRoot();
        const q = (sel) => (root.querySelector ? root.querySelector(sel) : null) || document.querySelector(sel);

        const b1 = q("#sb-step1-btn");
        const b2 = q("#sb-step2-btn");
        if (b1 && b1.__sbRender) b1.__sbRender(t);
        if (b2 && b2.__sbRender) b2.__sbRender(t);

        renderStageCanvas(q("#sb-anime-preview-canvas"), t);

        gCtx.clearRect(0, 0, window.innerWidth, window.innerHeight);

        for (let i = shockwaves.length - 1; i >= 0; i--) {
            const sw = shockwaves[i];
            sw.r += (sw.maxR - sw.r) * 0.16;
            sw.life -= 0.045;
            if (sw.life <= 0) { shockwaves.splice(i, 1); continue; }
            gCtx.save();
            gCtx.globalAlpha = Math.max(0, sw.life);
            gCtx.strokeStyle = sw.color;
            gCtx.lineWidth = 2.5 * sw.life;
            gCtx.beginPath();
            gCtx.arc(sw.x, sw.y, sw.r, 0, Math.PI * 2);
            gCtx.stroke();
            gCtx.restore();
        }

        for (let i = slashes.length - 1; i >= 0; i--) {
            const sl = slashes[i];
            sl.life -= 0.055;
            if (sl.life <= 0) { slashes.splice(i, 1); continue; }
            gCtx.save();
            gCtx.globalAlpha = Math.max(0, sl.life);
            gCtx.strokeStyle = sl.color;
            gCtx.shadowColor = sl.color;
            gCtx.shadowBlur = 20;
            gCtx.lineWidth = sl.width * sl.life;
            gCtx.lineCap = "round";
            gCtx.beginPath();
            gCtx.moveTo(sl.x1, sl.y1);
            gCtx.lineTo(sl.x2, sl.y2);
            gCtx.stroke();

            gCtx.strokeStyle = "#FFFFFF";
            gCtx.lineWidth = Math.max(1, sl.width * 0.35 * sl.life);
            gCtx.stroke();
            gCtx.restore();
        }

        for (let i = sparks.length - 1; i >= 0; i--) {
            const sp = sparks[i];
            sp.x += sp.vx;
            sp.y += sp.vy;
            sp.vx *= 0.95;
            sp.vy *= 0.95;
            sp.life -= 0.032;
            if (sp.life <= 0) { sparks.splice(i, 1); continue; }
            gCtx.save();
            gCtx.globalAlpha = Math.max(0, sp.life);
            gCtx.strokeStyle = sp.color;
            gCtx.shadowColor = sp.color;
            gCtx.shadowBlur = 8;
            gCtx.lineWidth = 2;
            gCtx.beginPath();
            gCtx.moveTo(sp.x, sp.y);
            gCtx.lineTo(sp.x - sp.vx * 1.8, sp.y - sp.vy * 1.8);
            gCtx.stroke();
            gCtx.restore();
        }

        for (let i = floatingTags.length - 1; i >= 0; i--) {
            const ft = floatingTags[i];
            ft.y += ft.vy;
            ft.life -= 0.024;
            if (ft.life <= 0) { floatingTags.splice(i, 1); continue; }
            gCtx.save();
            gCtx.globalAlpha = Math.max(0, ft.life);
            gCtx.font = "700 13px 'JetBrains Mono', monospace";
            gCtx.fillStyle = ft.color;
            gCtx.shadowColor = ft.color;
            gCtx.shadowBlur = 10;
            gCtx.textAlign = "center";
            gCtx.fillText(ft.text, ft.x, ft.y);
            gCtx.restore();
        }

        requestAnimationFrame(tick);
    };

    requestAnimationFrame(tick);
}
"""


# ==============================================================================
# BACKEND SETTINGS, PERSISTENT RAW EPISODE CACHE & LIVE PROGRESS PIPELINE
# ==============================================================================

def _rescue_legacy_tmp_downloads() -> None:
    """Rescue any completed episodes sitting in /workspace/tmp_downloads so user never re-downloads them."""
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
    title: str = "جاهز للانطلاق — أدخل الرابط أو اختر حلقة محفوظة للبدء",
    metric: str = "السرعة: — · المتبقي: —",
    detail: str = "الحلقات المحمّلة تُحفظ تلقائياً في /workspace/raw_episodes",
    mode: str = "cyan",
) -> str:
    clamped = max(0.0, min(100.0, float(pct)))
    mode_cls = {
        "violet": "sb-mode-violet",
        "done": "sb-mode-done",
        "error": "sb-mode-error",
    }.get(mode, "")
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
        "provider": "Gemini API",
        "llm_model": "gemini-2.5-flash",
        "api_key": os.environ.get("GEMINI_API_KEY", ""),
        "base_url": "",
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
    provider, llm_model, api_key, base_url, dood_api_key,
    mega_remote, mega_folder, gdrive_remote, gdrive_folder,
    codec, preset, cq, audio_bitrate,
    credit_translator, credit_typesetter, credit_encoder,
) -> str:
    data = {
        "provider": provider,
        "llm_model": llm_model,
        "api_key": api_key,
        "base_url": base_url,
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
    return f"✅ تم حفظ جميع المفاتيح والإعدادات في `{SETTINGS_FILE}` — سيتم تحميلها تلقائياً في كل مرة تفتح الواجهة."


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", name.strip())
    return cleaned or time.strftime("Episode_%Y%m%d_%H%M%S")


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
    """Deterministic persistent folder per magnet/URL/torrent so identical episodes NEVER re-download."""
    RAW_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if source_link and source_link.strip():
        s = source_link.strip()
        m_hash = re.search(r"btih:([a-zA-Z0-9]+)", s, re.IGNORECASE)
        if m_hash:
            key = f"magnet_{m_hash.group(1).lower()[:16]}"
        else:
            key = f"url_{hashlib.sha1(s.encode('utf-8')).hexdigest()[:14]}"
    elif torrent_file and os.path.isfile(torrent_file):
        h = hashlib.sha1(Path(torrent_file).read_bytes()).hexdigest()[:14]
        key = f"tor_{h}"
    else:
        key = _safe_name(ep_name)
    return RAW_CACHE_ROOT / f"{key}_{q_label}"


def step1_process_for_preview(
    episode_name: str,
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
):
    logs: List[str] = []
    rendered_map: Dict[str, str] = {}
    ass_out: Optional[str] = None

    def push(msg: str) -> str:
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        return "\n".join(logs[-150:])

    def pack(
        hud_html: str,
        preview_val=gr.update(),
        local_dd=gr.update(),
        raw_dd=gr.update(),
        vids_val=gr.update(),
        ass_val=gr.update(),
        state_val=gr.update(),
    ):
        return (hud_html, "\n".join(logs[-150:]), preview_val, local_dd, raw_dd, vids_val, ass_val, state_val)

    def run_live(
        fn: Callable,
        *args,
        badge: str,
        title: str,
        mode: str = "cyan",
        accepts_progress_cb: bool = True,
        **kwargs,
    ):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}

        def _log_cb(m: str) -> None:
            ev_q.put(("log", m))

        def _prog_cb(p: float, met: str, det: str) -> None:
            ev_q.put(("prog", (p, met, det)))

        kwargs["log_cb"] = _log_cb
        if accepts_progress_cb:
            kwargs["progress_cb"] = _prog_cb

        def _worker() -> None:
            try:
                res_box["val"] = fn(*args, **kwargs)
            except Exception as e:
                res_box["err"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        cur_pct = 0.0
        cur_met = "جاري البدء..."
        cur_det = ""
        dirty = True
        last_yield = 0.0

        while t.is_alive() or not ev_q.empty():
            try:
                kind, payload = ev_q.get(timeout=0.25)
                items = [(kind, payload)]
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
                elif k == "log":
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

    effective_file_1080 = file_1080
    if saved_raw_1080 and os.path.isfile(saved_raw_1080):
        effective_file_1080 = saved_raw_1080

    quality_inputs: List[Tuple[str, str, Optional[str]]] = []
    for label, lnk, fl in [
        ("1080p", link_1080, effective_file_1080),
        ("720p", link_720, file_720),
        ("480p", link_480, file_480),
    ]:
        if (lnk and lnk.strip()) or fl:
            quality_inputs.append((label, lnk, fl))

    if not quality_inputs:
        push("⚠️ أدخل رابط تورنت أو اختر حلقة خام محفوظة من القائمة قبل الانطلاق.")
        yield pack(
            render_progress_hud(0.0, "تنبيه · لا يوجد مصدر", "أدخل رابط أو اختر حلقة محفوظة من القائمة", "—", "—", mode="error"),
            state_val={},
        )
        return

    auto_base_name = episode_name.strip()
    if not auto_base_name and effective_file_1080 and os.path.isfile(effective_file_1080):
        auto_base_name = Path(effective_file_1080).stem
    ep_folder_name = _safe_name(auto_base_name)

    ep_out_dir = STORAGE_ROOT / ep_folder_name
    work_dir = RAW_CACHE_ROOT / "_work" / ep_folder_name
    ep_out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    push(f"⚔️ [STRIKE] بدء تجهيز الحلقة: {ep_folder_name}")
    yield pack(render_progress_hud(1.0, "STAGE 1/3 · INITIALIZING", f"بدء تجهيز الحلقة: {ep_folder_name}", "تجهيز المجلدات...", str(ep_out_dir)))

    try:
        downloaded: Dict[str, Path] = {}
        used_cache_dirs: List[Path] = []
        for q_label, lnk, fl in quality_inputs:
            cdir = _raw_cache_dir_for_source(q_label, lnk, fl, ep_folder_name)
            used_cache_dirs.append(cdir)
            vid_path = yield from run_live(
                download_torrent_or_url,
                lnk,
                fl,
                str(cdir),
                badge=f"STAGE 1/3 · DOWNLOAD [{q_label}]",
                title=f"تحميل / فحص الحلقة الخام بجودة {q_label}",
                accepts_progress_cb=True,
            )
            downloaded[q_label] = vid_path
            push(f"✓ جاهز ({q_label}): {vid_path.name}")

        updated_raw_list = [""] + list_cached_raw_videos()

        primary_q = quality_inputs[0][0]
        primary_vid = downloaded[primary_q]
        source_sub = None

        if sub_mode.startswith("Auto"):
            probe = probe_embedded_subtitles(str(primary_vid))
            if probe is not None:
                push(f"2/3 — سحب الترجمة المرفقة ({probe[1]}) من نسخة {primary_q}...")
                yield pack(
                    render_progress_hud(20.0, "STAGE 2/3 · SUBTITLE EXTRACT", f"سحب الترجمة المرفقة ({probe[1]}) من الفيديو...", "FFmpeg Demux", primary_vid.name),
                    raw_dd=gr.update(choices=updated_raw_list),
                )
                source_sub = extract_embedded_subtitle(str(primary_vid), probe[0], probe[1], str(work_dir))

        if source_sub is None:
            push(f"2/3 — تفريغ الصوت بـ Faster-Whisper ({whisper_model})...")
            source_sub = yield from run_live(
                transcribe_with_whisper,
                str(primary_vid),
                str(work_dir / "whisper.srt"),
                model_size=whisper_model,
                badge=f"STAGE 2/3 · FASTER-WHISPER [{whisper_model}]",
                title="تفريغ الصوت بالذكاء الاصطناعي إلى جمل زمنية...",
                accepts_progress_cb=False,
            )

        entries = parse_source_subtitles(source_sub)
        push(f"⚡ ترجمة {len(entries)} سطر للعربية عبر {provider} ({llm_model})...")
        translations = yield from run_live(
            translate_entries,
            entries=entries,
            provider="gemini" if provider.startswith("Gemini") else "openai",
            api_key=api_key,
            model_name=llm_model,
            base_url=base_url,
            badge="STAGE 2/3 · AI TRANSLATION",
            title=f"ترجمة {len(entries)} جملة للعربية عبر {llm_model}...",
            accepts_progress_cb=False,
        )

        credits_dict = {
            "translator": credit_translator,
            "typesetter": credit_typesetter,
            "encoder": credit_encoder,
        }
        ass_out = str(ep_out_dir / f"{ep_folder_name}_AR.ass")
        write_styled_ass(
            entries=entries,
            translations=translations,
            out_ass_path=ass_out,
            credits=credits_dict if any(v.strip() for v in credits_dict.values()) else None,
        )
        push(f"✓ تم إنشاء ملف الترجمة المنسّق: {Path(ass_out).name}")
        yield pack(
            render_progress_hud(100.0, "STAGE 2/3 · ASS READY", "اكتملت الترجمة وتنسيق الخطوط بنجاح!", "7 خطوط مفعّلة", Path(ass_out).name),
            raw_dd=gr.update(choices=updated_raw_list),
            ass_val=ass_out,
        )

        for q_label, vid_path in downloaded.items():
            out_mp4 = str(ep_out_dir / f"{ep_folder_name}_{q_label}_AR.mp4")
            yield from run_live(
                burn_and_compress_native,
                video_path=str(vid_path),
                ass_path=ass_out,
                output_path=out_mp4,
                fonts_dir=str(FONTS_DIR),
                codec=codec,
                cq=int(cq),
                preset=preset,
                audio_bitrate=audio_bitrate,
                badge=f"STAGE 3/3 · NVENC ENCODE [{q_label}]",
                title=f"حرق الترجمة وضغط جودة {q_label} بكرت الشاشة ({codec})...",
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

        push(
            f"✅ اكتمل التجهيز بنجاح! عاين الفيديو الآن، ثم اضغط زر (عبور البوابة S-PORTAL) للرفع.\n"
            f"📁 المخرجات: {ep_out_dir} | الحلقات الخام محفوظة في: {RAW_CACHE_ROOT}"
        )
        yield pack(
            render_progress_hud(
                100.0,
                "MISSION COMPLETE · 100%",
                "اكتمل التحميل والترجمة والحرق بنجاح! الفيديو جاهز للمعاينة أو الرفع",
                f"تم إنتاج {len(rendered_map)} جودة",
                str(ep_out_dir),
                mode="done",
            ),
            preview_val=first_video,
            local_dd=gr.update(choices=all_local, value=first_video),
            raw_dd=gr.update(choices=updated_raw_list),
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
                "راجع سجل العمليات للتفاصيل",
                "الحلقة الخام ما زالت محفوظة ولن يُعاد تحميلها!",
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
            render_progress_hud(0.0, "S-PORTAL · NO FILES", "شغّل المرحلة 1 [STRIKE] للمعاينة أولاً قبل الرفع.", "—", "—", mode="error"),
            "⚠️ لا توجد فيديوهات جاهزة للرفع حالياً. شغّل المرحلة 1 [STRIKE] للمعاينة أولاً.",
            "",
        )
        return

    logs: List[str] = []
    links_report: List[str] = [f"📁 مسار الحفظ المحلي على Runpod:\n  {job_state['folder']}\n"]

    def push(msg: str) -> str:
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        return "\n".join(logs[-150:])

    def run_upload_live(fn: Callable, *args, badge: str, title: str, **kwargs):
        ev_q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        res_box: Dict[str, object] = {}
        kwargs["log_cb"] = lambda m: ev_q.put(("log", m))
        kwargs["progress_cb"] = lambda p, met, det: ev_q.put(("prog", (p, met, det)))

        def _worker() -> None:
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

    files_map: Dict[str, str] = job_state["files"]

    try:
        for q_label, mp4_path in files_map.items():
            links_report.append(f"=== جودة {q_label} ({Path(mp4_path).name}) ===")
            links_report.append(f"  Local Path: {mp4_path}")

            if enable_mega:
                push(f"🌀 جاري رفع {q_label} على MEGA...")
                mega_link = yield from run_upload_live(
                    upload_to_rclone,
                    mp4_path, mega_remote, mega_folder, generate_link=True,
                    badge=f"S-PORTAL · MEGA [{q_label}]",
                    title=f"جاري رفع نسخة {q_label} على سحابة MEGA...",
                )
                links_report.append(f"  MEGA: {mega_link}")

            if enable_dood:
                push(f"🌀 جاري رفع {q_label} على DoodStream...")
                dood_res = yield from run_upload_live(
                    upload_to_doodstream,
                    mp4_path, dood_api_key,
                    badge=f"S-PORTAL · DOODSTREAM [{q_label}]",
                    title=f"جاري رفع نسخة {q_label} على DoodStream...",
                )
                links_report.append(f"  DoodStream Watch: {dood_res['watch_url']}")
                links_report.append(f"  DoodStream Embed: {dood_res['embed_url']}")

            if enable_gdrive:
                push(f"🌀 جاري رفع {q_label} على Google Drive...")
                gd_res = yield from run_upload_live(
                    upload_to_rclone,
                    mp4_path, gdrive_remote, gdrive_folder, generate_link=False,
                    badge=f"S-PORTAL · GOOGLE DRIVE [{q_label}]",
                    title=f"جاري رفع نسخة {q_label} على Google Drive...",
                )
                links_report.append(f"  Google Drive: {gd_res}")

            links_report.append("")

        (Path(job_state["folder"]) / "links.txt").write_text("\n".join(links_report), encoding="utf-8")
        push("✅ اكتمل الرفع السحابي وحُفظت جميع الروابط داخل links.txt!")
        yield (
            render_progress_hud(100.0, "S-PORTAL · UPLOAD COMPLETE", "اكتمل رفع جميع الجودات واستخراج الروابط بنجاح!", "100% Cloud Sync", job_state["folder"], mode="done"),
            "\n".join(logs[-150:]),
            "\n".join(links_report),
        )
    except Exception as exc:
        push(f"❌ خطأ أثناء الرفع: {exc}\n{traceback.format_exc()[-400:]}")
        yield (
            render_progress_hud(0.0, "S-PORTAL · UPLOAD ERROR", f"خطأ أثناء الرفع: {str(exc)[:100]}", "راجع سجل العمليات", "", mode="error"),
            "\n".join(logs[-150:]),
            "\n".join(links_report),
        )


# ==============================================================================
# SHADOW BLADE GRADIO UI BUILDER
# ==============================================================================

def _build_blocks_kwargs() -> Dict:
    sig = inspect.signature(gr.Blocks.__init__).parameters
    kwargs: Dict = {"title": "نصل الظل · سَبّر الأنمي | Shadow Blade Subber"}
    theme_obj = gr.themes.Base(
        primary_hue="sky",
        secondary_hue="violet",
        neutral_hue="slate",
    )
    if "theme" in sig:
        kwargs["theme"] = theme_obj
    if "css" in sig:
        kwargs["css"] = SHADOW_BLADE_CSS
    if "head" in sig:
        kwargs["head"] = SHADOW_BLADE_HEAD
    if "js" in sig:
        kwargs["js"] = SHADOW_BLADE_JS
    return kwargs


def build_ui() -> gr.Blocks:
    cfg = load_settings()

    with gr.Blocks(**_build_blocks_kwargs()) as demo:
        gr.HTML(f"<style>{SHADOW_BLADE_CSS}</style>{SHADOW_BLADE_HEAD}", visible=True)
        gr.HTML(SHADOW_BLADE_HERO_HTML)

        job_state = gr.State({})

        with gr.Row():
            with gr.Column(scale=2):
                episode_name = gr.Textbox(
                    label="📌 اسم الحلقة / مجلد الإخراج",
                    placeholder="مثال: Solo_Action_S2_EP01 (أو اتركه فارغاً ليأخذ اسم الحلقة تلقائياً)",
                )

                with gr.Accordion(
                    "🔗 الحلقات المحفوظة على السيرفر أو روابط التورنت (حفظ دائم بدون إعادة تحميل)",
                    open=True,
                    elem_classes=["sb-accordion"],
                ):
                    with gr.Row():
                        saved_raw_1080 = gr.Dropdown(
                            choices=[""] + list_cached_raw_videos(),
                            value="",
                            label="📦 اختر حلقة خام محمّلة مسبقاً على السيرفر (/workspace/raw_episodes) — تتخطى التحميل فوراً!",
                            interactive=True,
                            scale=4,
                        )
                        refresh_raw_btn = gr.Button("🔄 تحديث المحفوظ", size="sm", elem_id="sb-refresh-btn", scale=1)

                    with gr.Row():
                        link_1080 = gr.Textbox(label="رابط 1080p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_1080 = gr.File(label="أو ملف .torrent / فيديو 1080p", type="filepath")
                    with gr.Row():
                        link_720 = gr.Textbox(label="رابط 720p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_720 = gr.File(label="أو ملف .torrent / فيديو 720p", type="filepath")
                    with gr.Row():
                        link_480 = gr.Textbox(label="رابط 480p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_480 = gr.File(label="أو ملف .torrent / فيديو 480p", type="filepath")

                with gr.Accordion(
                    "🔑 المفاتيح والإعدادات الدائمة (تُحفظ تلقائياً على السيرفر)",
                    open=True,
                    elem_classes=["sb-accordion"],
                ):
                    with gr.Row():
                        provider = gr.Dropdown(
                            choices=["Gemini API", "OpenAI-Compatible / Local"],
                            value=cfg["provider"],
                            label="محرك الترجمة الذكي",
                        )
                        llm_model = gr.Textbox(value=cfg["llm_model"], label="اسم الموديل")
                    with gr.Row():
                        api_key = gr.Textbox(
                            value=cfg["api_key"],
                            label="Gemini / OpenAI API Key",
                            type="password",
                        )
                        base_url = gr.Textbox(
                            value=cfg["base_url"],
                            label="Base URL (اختياري للموديل المحلي)",
                        )
                    with gr.Row():
                        dood_api_key = gr.Textbox(
                            value=cfg["dood_api_key"],
                            label="DoodStream API Key",
                            type="password",
                        )
                        mega_remote = gr.Textbox(value=cfg["mega_remote"], label="MEGA Remote")
                        mega_folder = gr.Textbox(value=cfg["mega_folder"], label="فولدر MEGA")
                    with gr.Row():
                        gdrive_remote = gr.Textbox(value=cfg["gdrive_remote"], label="Google Drive Remote")
                        gdrive_folder = gr.Textbox(value=cfg["gdrive_folder"], label="فولدر Drive")

                    save_btn = gr.Button(
                        "💾 حفظ جميع المفاتيح والإعدادات في النظام",
                        variant="secondary",
                        elem_id="sb-save-btn",
                    )
                    save_status = gr.Markdown("")

                with gr.Accordion(
                    "⚙️ إعدادات الترجمة والضغط (NVENC) وحقوق الفريق",
                    open=False,
                    elem_classes=["sb-accordion"],
                ):
                    with gr.Row():
                        sub_mode = gr.Radio(
                            choices=[
                                "Auto (سحب ترجمة MKV إن وجدت، وإلا Whisper)",
                                "Force Whisper (تفريغ صوتي دائماً)",
                            ],
                            value="Auto (سحب ترجمة MKV إن وجدت، وإلا Whisper)",
                            label="مصدر الترجمة",
                        )
                        whisper_model = gr.Dropdown(
                            choices=["large-v3", "medium", "small"],
                            value="large-v3",
                            label="موديل Whisper",
                        )
                    with gr.Row():
                        codec = gr.Dropdown(
                            choices=["h264_nvenc", "hevc_nvenc", "av1_nvenc"],
                            value=cfg["codec"],
                            label="الكوديك (GPU NVENC)",
                        )
                        preset = gr.Dropdown(
                            choices=["p7", "p6", "p5", "p4"],
                            value=cfg["preset"],
                            label="دقة الضغط",
                        )
                        cq = gr.Slider(
                            minimum=18,
                            maximum=30,
                            step=1,
                            value=cfg["cq"],
                            label="الجودة CQ",
                        )
                        audio_bitrate = gr.Dropdown(
                            choices=["128k", "160k", "192k"],
                            value=cfg["audio_bitrate"],
                            label="صوت AAC",
                        )
                    with gr.Row():
                        credit_translator = gr.Textbox(value=cfg["credit_translator"], label="الترجمة")
                        credit_typesetter = gr.Textbox(value=cfg["credit_typesetter"], label="المحاكاة")
                        credit_encoder = gr.Textbox(value=cfg["credit_encoder"], label="الدمج والرفع")

                    cleanup_temp = gr.Checkbox(
                        value=False,
                        label="مسح الحلقة الخام بعد انتهاء الدمج (اتركه مغلقاً للاحتفاظ بالحلقات المحمّلة دائماً وعدم إعادة تحميلها)",
                    )

                progress_hud = gr.HTML(value=render_progress_hud(), elem_id="sb-progress-hud")

                step1_btn = gr.Button(
                    "⚔️ انطلق — بدء التحميل والترجمة والدمج للمعاينة! [STRIKE]",
                    variant="primary",
                    size="lg",
                    elem_id="sb-step1-btn",
                )

                with gr.Row():
                    enable_mega = gr.Checkbox(value=True, label="رفع على MEGA")
                    enable_dood = gr.Checkbox(value=True, label="رفع على DoodStream")
                    enable_gdrive = gr.Checkbox(value=False, label="رفع على Google Drive")

                step2_btn = gr.Button(
                    "🌀 عبور البوابة — اعتماد ورفع جميع الجودات [S-PORTAL]",
                    variant="stop",
                    size="lg",
                    elem_id="sb-step2-btn",
                )

            with gr.Column(scale=1):
                gr.HTML(SHADOW_BLADE_STAGE_HTML)

                preview_player = gr.Video(
                    label="📺 مشغّل معاينة الحلقة بعد الحرق (تأكد من الخط والتوقيت قبل الرفع)",
                    elem_id="sb-video-player",
                )

                with gr.Row():
                    local_dropdown = gr.Dropdown(
                        choices=list_local_outputs(),
                        label="📂 تصفّح الحلقات المحروقة الجاهزة (/workspace/outputs)",
                        interactive=True,
                    )
                    refresh_local_btn = gr.Button("🔄 تحديث", size="sm", elem_id="sb-refresh-btn")

                links_box = gr.Textbox(
                    label="🔗 روابط الرفع الجاهزة للنشر (MEGA / DoodStream / Drive)",
                    lines=8,
                    interactive=False,
                    elem_id="sb-links-box",
                )
                log_box = gr.Textbox(
                    label="📋 سجل عمليات النظام المباشر [HUNTER LOG]",
                    lines=14,
                    max_lines=20,
                    interactive=False,
                    elem_id="sb-log-box",
                )
                out_video_files = gr.Files(label="🎥 تحميل الفيديوهات النهائية لجهازك")
                out_ass_file = gr.File(label="📝 تحميل ملف الترجمة المنسّق (.ass)")

        load_sig = inspect.signature(demo.load).parameters
        if "js" in load_sig:
            demo.load(fn=None, inputs=None, outputs=None, js=SHADOW_BLADE_JS)

        save_btn.click(
            fn=save_settings,
            inputs=[
                provider, llm_model, api_key, base_url, dood_api_key,
                mega_remote, mega_folder, gdrive_remote, gdrive_folder,
                codec, preset, cq, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
            ],
            outputs=[save_status],
        )

        refresh_raw_btn.click(
            fn=lambda: gr.update(choices=[""] + list_cached_raw_videos()),
            outputs=[saved_raw_1080],
        )

        refresh_local_btn.click(
            fn=lambda: gr.update(choices=list_local_outputs()),
            outputs=[local_dropdown],
        )
        local_dropdown.change(fn=lambda p: p, inputs=[local_dropdown], outputs=[preview_player])

        step1_btn.click(
            fn=step1_process_for_preview,
            inputs=[
                episode_name,
                saved_raw_1080,
                link_1080, file_1080,
                link_720, file_720,
                link_480, file_480,
                sub_mode, whisper_model, provider, api_key, llm_model, base_url,
                codec, cq, preset, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
                cleanup_temp,
            ],
            outputs=[progress_hud, log_box, preview_player, local_dropdown, saved_raw_1080, out_video_files, out_ass_file, job_state],
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
    if "js" in launch_sig:
        launch_kwargs["js"] = SHADOW_BLADE_JS

    ui.queue().launch(**launch_kwargs)
