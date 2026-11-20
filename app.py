import json
import os
import re
import shutil
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gradio as gr

from media_ops import (
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
    write_styled_ass,
)

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"
STORAGE_ROOT = Path(os.environ.get("RUNPOD_OUTPUT_ROOT", "/workspace/outputs"))
TEMP_ROOT = Path(os.environ.get("RUNPOD_TEMP_ROOT", "/workspace/tmp_downloads"))
SETTINGS_FILE = Path(os.environ.get("RUNPOD_SETTINGS_FILE", "/workspace/subber_settings.json"))


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
    return f"✅ تم حفظ جميع المفاتيح والإعدادات في {SETTINGS_FILE} — هتتحمّل تلقائياً في كل مرة تفتح الواجهة."


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", name.strip())
    return cleaned or time.strftime("Episode_%Y%m%d_%H%M%S")


def list_local_outputs() -> List[str]:
    if not STORAGE_ROOT.exists():
        return []
    mp4s = sorted(STORAGE_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in mp4s[:50]]


def step1_process_for_preview(
    episode_name: str,
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

    quality_inputs: List[Tuple[str, str, Optional[str]]] = []
    for label, lnk, fl in [
        ("1080p", link_1080, file_1080),
        ("720p", link_720, file_720),
        ("480p", link_480, file_480),
    ]:
        if (lnk and lnk.strip()) or fl:
            quality_inputs.append((label, lnk, fl))

    if not quality_inputs:
        yield push("⚠️ أدخل رابط أو ملف تورنت لجودة واحدة على الأقل."), None, gr.update(), None, None, {}
        return

    ep_folder_name = _safe_name(episode_name)
    ep_out_dir = STORAGE_ROOT / ep_folder_name
    ep_tmp_dir = TEMP_ROOT / f"{ep_folder_name}_{time.strftime('%H%M%S')}"
    ep_out_dir.mkdir(parents=True, exist_ok=True)
    ep_tmp_dir.mkdir(parents=True, exist_ok=True)

    yield push(f"بدء التجهيز للمعاينة (بدون رفع): {ep_folder_name}"), None, gr.update(), None, None, {}

    try:
        # 1. Download all provided qualities
        downloaded: Dict[str, Path] = {}
        for q_label, lnk, fl in quality_inputs:
            yield push(f"1/3 — تحميل جودة {q_label}..."), None, gr.update(), None, None, {}
            vid_path = download_torrent_or_url(lnk, fl, str(ep_tmp_dir / q_label), log_cb=lambda m: push(m))
            downloaded[q_label] = vid_path
            yield push(f"تم تحميل {q_label}: {vid_path.name}"), None, gr.update(), None, None, {}

        # 2. Extract & Translate ONCE
        primary_q = quality_inputs[0][0]
        primary_vid = downloaded[primary_q]
        source_sub = None
        if sub_mode.startswith("Auto"):
            probe = probe_embedded_subtitles(str(primary_vid))
            if probe is not None:
                yield push(f"2/3 — سحب الترجمة المرفقة ({probe[1]}) من {primary_q}..."), None, gr.update(), None, None, {}
                source_sub = extract_embedded_subtitle(str(primary_vid), probe[0], probe[1], str(ep_tmp_dir))

        if source_sub is None:
            yield push(f"2/3 — تفريغ الصوت بـ Faster-Whisper ({whisper_model})..."), None, gr.update(), None, None, {}
            source_sub = transcribe_with_whisper(
                str(primary_vid), str(ep_tmp_dir / "whisper.srt"), model_size=whisper_model, log_cb=lambda m: push(m)
            )

        entries = parse_source_subtitles(source_sub)
        yield push(f"ترجمة {len(entries)} سطر للعربية عبر {provider} ({llm_model})..."), None, gr.update(), None, None, {}
        translations = translate_entries(
            entries=entries,
            provider="gemini" if provider.startswith("Gemini") else "openai",
            api_key=api_key,
            model_name=llm_model,
            base_url=base_url,
            log_cb=lambda m: push(m),
        )

        credits_dict = {"translator": credit_translator, "typesetter": credit_typesetter, "encoder": credit_encoder}
        ass_out = str(ep_out_dir / f"{ep_folder_name}_AR.ass")
        write_styled_ass(
            entries=entries,
            translations=translations,
            out_ass_path=ass_out,
            credits=credits_dict if any(v.strip() for v in credits_dict.values()) else None,
        )
        yield push(f"تم إنشاء ملف الترجمة: {Path(ass_out).name}"), None, gr.update(), None, ass_out, {}

        # 3. Burn & compress each quality (NO UPLOAD YET)
        for q_label, vid_path in downloaded.items():
            out_mp4 = str(ep_out_dir / f"{ep_folder_name}_{q_label}_AR.mp4")
            yield push(f"3/3 — حرق الترجمة وضغط {q_label} ({codec}, CQ={cq})..."), None, gr.update(), list(rendered_map.values()) or None, ass_out, rendered_map
            burn_and_compress_native(
                video_path=str(vid_path),
                ass_path=ass_out,
                output_path=out_mp4,
                fonts_dir=str(FONTS_DIR),
                codec=codec,
                cq=int(cq),
                preset=preset,
                audio_bitrate=audio_bitrate,
                log_cb=lambda m: push(m),
            )
            rendered_map[q_label] = out_mp4

        if cleanup_temp and ep_tmp_dir.exists():
            shutil.rmtree(ep_tmp_dir, ignore_errors=True)

        first_video = next(iter(rendered_map.values()))
        all_local = list_local_outputs()
        yield (
            push(f"✅ التجهيز خلص! عاين الفيديو في المشغّل دلوقتي، ولو تمام اضغط زر (اعتماد ورفع).\n📁 محفوظ في: {ep_out_dir}"),
            first_video,
            gr.update(choices=all_local, value=first_video),
            list(rendered_map.values()),
            ass_out,
            {"folder": str(ep_out_dir), "files": rendered_map},
        )

    except Exception as exc:
        yield push(f"❌ حدث خطأ: {exc}\n{traceback.format_exc()[-500:]}"), None, gr.update(), list(rendered_map.values()) or None, ass_out, {}


def step2_approve_and_upload(
    job_state: Dict,
    enable_mega: bool, mega_remote: str, mega_folder: str,
    enable_dood: bool, dood_api_key: str,
    enable_gdrive: bool, gdrive_remote: str, gdrive_folder: str,
):
    if not job_state or not job_state.get("files"):
        yield "⚠️ مفيش فيديوهات جاهزة للرفع. شغّل الخطوة 1 (المعاينة) الأول.", ""
        return

    logs: List[str] = []
    links_report: List[str] = [f"📁 مسار الحفظ المحلي على Runpod:\n   {job_state['folder']}\n"]

    def push(msg: str) -> str:
        logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        return "\n".join(logs[-150:])

    files_map: Dict[str, str] = job_state["files"]

    try:
        for q_label, mp4_path in files_map.items():
            links_report.append(f"=== جودة {q_label} ({Path(mp4_path).name}) ===")
            links_report.append(f"  Local Path: {mp4_path}")

            if enable_mega:
                yield push(f"جاري رفع {q_label} على MEGA..."), "\n".join(links_report)
                mega_link = upload_to_rclone(mp4_path, mega_remote, mega_folder, generate_link=True, log_cb=lambda m: push(m))
                links_report.append(f"  MEGA: {mega_link}")

            if enable_dood:
                yield push(f"جاري رفع {q_label} على DoodStream..."), "\n".join(links_report)
                dood_res = upload_to_doodstream(mp4_path, dood_api_key, log_cb=lambda m: push(m))
                links_report.append(f"  DoodStream Watch: {dood_res['watch_url']}")
                links_report.append(f"  DoodStream Embed: {dood_res['embed_url']}")

            if enable_gdrive:
                yield push(f"جاري رفع {q_label} على Google Drive..."), "\n".join(links_report)
                gd_res = upload_to_rclone(mp4_path, gdrive_remote, gdrive_folder, generate_link=False, log_cb=lambda m: push(m))
                links_report.append(f"  Google Drive: {gd_res}")

            links_report.append("")
            yield "\n".join(logs[-150:]), "\n".join(links_report)

        (Path(job_state["folder"]) / "links.txt").write_text("\n".join(links_report), encoding="utf-8")
        yield push("✅ تم الرفع بنجاح وحفظ كل الروابط في links.txt!"), "\n".join(links_report)

    except Exception as exc:
        yield push(f"❌ خطأ أثناء الرفع: {exc}\n{traceback.format_exc()[-400:]}"), "\n".join(links_report)


def build_ui() -> gr.Blocks:
    cfg = load_settings()
    job_state = gr.State({})

    with gr.Blocks(title="Runpod Anime Subber — Preview & Upload", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
            # 🎬 Runpod Anime Subber (معاينة قبل الرفع + حفظ المفاتيح + متصفّح الملفات المحلية)
            **الخطوة 1:** حمّل وترجم وادمج وعاين الفيديو في المشغّل الأول ← **الخطوة 2:** لو كله تمام اضغط **اعتماد ورفع** على MEGA و DoodStream.
            """
        )

        with gr.Row():
            with gr.Column(scale=2):
                episode_name = gr.Textbox(
                    label="📌 اسم الحلقة / الفولدر",
                    placeholder="مثال: Black_Clover_S2_EP01",
                )

                with gr.Accordion("🔗 روابط أو ملفات التورنت لكل جودة (سيب اللي مش عايزه فاضي)", open=True):
                    with gr.Row():
                        link_1080 = gr.Textbox(label="رابط 1080p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_1080 = gr.File(label="أو ملف .torrent / فيديو 1080p", type="filepath")
                    with gr.Row():
                        link_720 = gr.Textbox(label="رابط 720p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_720 = gr.File(label="أو ملف .torrent / فيديو 720p", type="filepath")
                    with gr.Row():
                        link_480 = gr.Textbox(label="رابط 480p (Magnet / URL)", placeholder="magnet:?xt=...")
                        file_480 = gr.File(label="أو ملف .torrent / فيديو 480p", type="filepath")

                with gr.Accordion("🔑 المفاتيح والإعدادات الدائمة (احفظها مرة واحدة بس)", open=True):
                    with gr.Row():
                        provider = gr.Dropdown(choices=["Gemini API", "OpenAI-Compatible / Local"], value=cfg["provider"], label="محرك الترجمة")
                        llm_model = gr.Textbox(value=cfg["llm_model"], label="اسم الموديل")
                    with gr.Row():
                        api_key = gr.Textbox(value=cfg["api_key"], label="Gemini / OpenAI API Key", type="password")
                        base_url = gr.Textbox(value=cfg["base_url"], label="Base URL (اختياري للموديل المحلي)")
                    with gr.Row():
                        dood_api_key = gr.Textbox(value=cfg["dood_api_key"], label="DoodStream API Key (من Settings داخل حسابك)", type="password")
                        mega_remote = gr.Textbox(value=cfg["mega_remote"], label="MEGA Remote")
                        mega_folder = gr.Textbox(value=cfg["mega_folder"], label="فولدر MEGA")
                    with gr.Row():
                        gdrive_remote = gr.Textbox(value=cfg["gdrive_remote"], label="Google Drive Remote")
                        gdrive_folder = gr.Textbox(value=cfg["gdrive_folder"], label="فولدر Drive")
                    save_btn = gr.Button("💾 حفظ جميع المفاتيح والإعدادات (عشان متكتبهاش تاني)", variant="secondary")
                    save_status = gr.Markdown("")

                with gr.Accordion("⚙️ إعدادات الترجمة والضغط والحقوق", open=False):
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
                        codec = gr.Dropdown(choices=["h264_nvenc", "hevc_nvenc", "av1_nvenc"], value=cfg["codec"], label="الكوديك")
                        preset = gr.Dropdown(choices=["p7", "p6", "p5", "p4"], value=cfg["preset"], label="دقة الضغط")
                        cq = gr.Slider(minimum=18, maximum=30, step=1, value=cfg["cq"], label="الجودة CQ")
                        audio_bitrate = gr.Dropdown(choices=["128k", "160k", "192k"], value=cfg["audio_bitrate"], label="صوت AAC")
                    with gr.Row():
                        credit_translator = gr.Textbox(value=cfg["credit_translator"], label="الترجمة")
                        credit_typesetter = gr.Textbox(value=cfg["credit_typesetter"], label="المحاكاة")
                        credit_encoder = gr.Textbox(value=cfg["credit_encoder"], label="الدمج والرفع")
                    cleanup_temp = gr.Checkbox(value=True, label="مسح ملفات التورنت الخام بعد التجهيز")

                step1_btn = gr.Button("🎬 1. تحميل وترجمة وضغط للمعاينة (بدون رفع)", variant="primary", size="lg")

                with gr.Row():
                    enable_mega = gr.Checkbox(value=True, label="رفع على MEGA")
                    enable_dood = gr.Checkbox(value=True, label="رفع على DoodStream")
                    enable_gdrive = gr.Checkbox(value=False, label="رفع على Google Drive")
                step2_btn = gr.Button("☁️ 2. اعتماد ورفع الفيديوهات بعد المعاينة", variant="stop", size="lg")

            with gr.Column(scale=1):
                preview_player = gr.Video(label="📺 معاينة الفيديو قبل الرفع (اتأكد من الخط والتوقيت)")
                with gr.Row():
                    local_dropdown = gr.Dropdown(
                        choices=list_local_outputs(),
                        label="📂 تصفّح الملفات المحفوظة على Runpod (/workspace/outputs)",
                        interactive=True,
                    )
                    refresh_local_btn = gr.Button("🔄 تحديث", size="sm")
                links_box = gr.Textbox(label="🔗 روابط الرفع الجاهزة (MEGA / DoodStream)", lines=8, interactive=False)
                log_box = gr.Textbox(label="📋 سجل العمليات المباشر", lines=14, max_lines=20, interactive=False)
                out_video_files = gr.Files(label="🎥 تحميل الفيديوهات النهائية لجهازك")
                out_ass_file = gr.File(label="📝 تحميل ملف الترجمة (.ass)")

        # Wire events
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

        refresh_local_btn.click(
            fn=lambda: gr.update(choices=list_local_outputs()),
            outputs=[local_dropdown],
        )
        local_dropdown.change(fn=lambda p: p, inputs=[local_dropdown], outputs=[preview_player])

        step1_btn.click(
            fn=step1_process_for_preview,
            inputs=[
                episode_name,
                link_1080, file_1080,
                link_720, file_720,
                link_480, file_480,
                sub_mode, whisper_model, provider, api_key, llm_model, base_url,
                codec, cq, preset, audio_bitrate,
                credit_translator, credit_typesetter, credit_encoder,
                cleanup_temp,
            ],
            outputs=[log_box, preview_player, local_dropdown, out_video_files, out_ass_file, job_state],
        )

        step2_btn.click(
            fn=step2_approve_and_upload,
            inputs=[
                job_state,
                enable_mega, mega_remote, mega_folder,
                enable_dood, dood_api_key,
                enable_gdrive, gdrive_remote, gdrive_folder,
            ],
            outputs=[log_box, links_box],
        )

    return demo


if __name__ == "__main__":
    ui = build_ui()
    user, pwd = os.environ.get("UI_USER", "").strip(), os.environ.get("UI_PASS", "").strip()
    ui.queue().launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", "7860")),
        share=os.environ.get("GRADIO_SHARE", "false").lower() == "true",
        auth=(user, pwd) if (user and pwd) else None,
    )
