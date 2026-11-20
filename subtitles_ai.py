import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pysubs2

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "style_template.ass"
TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text"}

SYSTEM_PROMPT = """You are an expert Arabic anime fansubber (مترجم أنمي محترف).
Translate the provided JSON array of subtitle items into fluent, natural, dramatic Modern Standard Arabic (العربية الفصحى السلسة المناسبة للأنمي).
Rules:
1. Return ONLY a valid JSON object mapping each integer "id" (as a string key) to its Arabic translation string.
2. Preserve meaning, anime terminology, tone, and line breaks (\\N).
3. For style "Narrator" or "Narrator - Top", use eloquent narrative Arabic.
4. For style "OP-Ara" or "ED-Ara", use poetic Arabic lyrics.
5. For style "Signs", keep translations concise so they fit cleanly on screen.
6. Output valid JSON only, with no markdown fences or commentary."""


@dataclass
class SubEntry:
    index: int
    start_ms: int
    end_ms: int
    target_style: str
    actor: str
    plain_text: str
    preserved_tags: str
    keep_untranslated: bool = False


def load_ass_header(template_path: Path = TEMPLATE_PATH) -> str:
    """Load [Script Info], [V4+ Styles], and [Events] header from style_template.ass."""
    if not template_path.is_file():
        raise FileNotFoundError(
            f"Missing style template: {template_path}. Make sure style_template.ass is in the repo root."
        )
    text = template_path.read_text(encoding="utf-8-sig")
    if "[Events]" not in text:
        raise ValueError("style_template.ass must contain an [Events] section.")
    return text.rstrip("\n")


def probe_embedded_subtitles(video_path: str) -> Optional[Tuple[int, str]]:
    """Find the best embedded English/text subtitle stream in an MKV/MP4."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title",
        "-of", "json",
        video_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return None

    streams = json.loads(res.stdout or "{}").get("streams", [])
    candidates = []
    for pos, st in enumerate(streams):
        codec = (st.get("codec_name") or "").lower()
        if codec not in TEXT_SUB_CODECS:
            continue
        tags = st.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        title = (tags.get("title") or "").lower()
        score = (10 if lang in ("eng", "en", "english") else 0) + (5 if codec in ("ass", "ssa") else 0)
        if "sign" in title and "full" not in title:
            score -= 6
        candidates.append((score, pos, codec))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def extract_embedded_subtitle(video_path: str, sub_pos: int, codec: str, out_dir: str) -> str:
    ext = ".ass" if codec in ("ass", "ssa") else ".srt"
    out_path = os.path.join(out_dir, f"extracted_source{ext}")
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-map", f"0:s:{sub_pos}", out_path],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError(f"Failed to extract subtitle: {res.stderr[-300:]}")
    return out_path


def transcribe_with_whisper(
    video_path: str,
    out_srt_path: str,
    model_size: str = "large-v3",
    log_cb: Optional[Callable[[str], None]] = None,
) -> str:
    from faster_whisper import WhisperModel

    device, compute_type = "cuda", "float16"
    try:
        import torch
        if not torch.cuda.is_available():
            device, compute_type = "cpu", "int8"
    except Exception:
        pass

    if log_cb:
        log_cb(f"Loading faster-whisper ({model_size}) on {device}...")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(video_path, beam_size=5, vad_filter=True)
    if log_cb:
        log_cb(f"Detected language: {info.language} ({info.language_probability:.2f})")

    subs = pysubs2.SSAFile()
    for seg in segments:
        txt = seg.text.strip()
        if txt:
            subs.append(pysubs2.SSAEvent(start=int(seg.start * 1000), end=int(seg.end * 1000), text=txt, style="Main"))
    subs.save(out_srt_path)
    return out_srt_path


def _classify_style(orig_style: str, orig_actor: str, raw_text: str) -> Tuple[str, bool]:
    s, a = (orig_style or "").lower(), (orig_actor or "").lower()
    if "rom" in s or "kara" in s:
        return ("ED-Rom" if ("ed" in s or "end" in s) else "OP-Rom"), True
    if any(k in s for k in ("op", "opening")):
        return "OP-Ara", False
    if any(k in s for k in ("ed", "ending")):
        return "ED-Ara", False
    if "narrat" in s or "narrat" in a:
        return ("Narrator - Top" if ("top" in s or "\\an8" in raw_text) else "Narrator"), False
    if any(k in s for k in ("sign", "title", "caption", "screen", "board")) or "sign" in a or "\\pos(" in raw_text or "\\move(" in raw_text:
        return "Signs", False
    if "top" in s or "overlap" in s or "\\an8" in raw_text:
        return "Top", False
    return "Main", False


def _extract_layout_tags(raw_text: str) -> str:
    tags = []
    for block in re.findall(r"\{([^}]*)\}", raw_text):
        for m in re.finditer(r"\\(?:pos|move|fad|fade|an|org|frz|blur|bord|fs)\([^)]*\)|\\(?:an\d|blur[\d.]+|fs[\d.]+)", block):
            tags.append(m.group(0))
    return "".join(tags)


def parse_source_subtitles(sub_path: str) -> List[SubEntry]:
    subs = pysubs2.load(sub_path, encoding="utf-8")
    entries: List[SubEntry] = []
    for ev in subs:
        if ev.is_comment:
            continue
        raw = ev.text.strip()
        if not raw or re.search(r"\\p[1-9]", raw):
            continue
        target_style, keep_untranslated = _classify_style(ev.style, ev.name, raw)
        plain = re.sub(r"\{[^}]*\}", "", raw).strip()
        if not plain:
            continue
        entries.append(
            SubEntry(
                index=len(entries),
                start_ms=ev.start,
                end_ms=ev.end,
                target_style=target_style,
                actor=ev.name or "",
                plain_text=plain,
                preserved_tags=_extract_layout_tags(raw) if target_style == "Signs" else "",
                keep_untranslated=keep_untranslated,
            )
        )
    return entries


def _parse_json_response(raw: str) -> Dict[int, str]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e != -1:
        cleaned = cleaned[s : e + 1]
    return {int(k): str(v) for k, v in json.loads(cleaned).items()}


def translate_entries(
    entries: List[SubEntry],
    provider: str = "gemini",
    api_key: str = "",
    model_name: str = "gemini-2.5-flash",
    base_url: str = "",
    batch_size: int = 35,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[int, str]:
    to_translate = [e for e in entries if not e.keep_untranslated]
    if not to_translate:
        return {}
    if provider == "gemini" and not api_key.strip():
        raise ValueError("Please enter your Gemini API Key.")

    translations: Dict[int, str] = {}
    total_batches = (len(to_translate) + batch_size - 1) // batch_size

    for b_idx in range(total_batches):
        chunk = to_translate[b_idx * batch_size : (b_idx + 1) * batch_size]
        payload = json.dumps(
            [{"id": e.index, "style": e.target_style, "actor": e.actor, "text": e.plain_text} for e in chunk],
            ensure_ascii=False,
        )
        for attempt in range(3):
            try:
                if provider == "gemini":
                    from google import genai
                    from google.genai import types
                    client = genai.Client(api_key=api_key.strip())
                    resp = client.models.generate_content(
                        model=model_name.strip(),
                        contents=payload,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            temperature=0.3,
                            response_mime_type="application/json",
                        ),
                    )
                    translations.update(_parse_json_response(resp.text or "{}"))
                else:
                    from openai import OpenAI
                    client = OpenAI(api_key=api_key.strip() or "local-key", base_url=base_url.strip() or None)
                    resp = client.chat.completions.create(
                        model=model_name.strip(),
                        temperature=0.3,
                        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": payload}],
                    )
                    translations.update(_parse_json_response(resp.choices[0].message.content or "{}"))
                break
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(f"Translation failed on batch {b_idx + 1}: {exc}")
                time.sleep(2 * (attempt + 1))

        if log_cb:
            log_cb(f"Translated batch {b_idx + 1}/{total_batches}")

    return translations


def _ms_to_ass_time(ms: int) -> str:
    ms = max(0, int(ms))
    return f"{ms // 3600000}:{(ms // 60000) % 60:02d}:{(ms // 1000) % 60:02d}.{(ms // 10) % 100:02d}"


def _format_event_text(entry: SubEntry, translated_text: str) -> str:
    text = translated_text.replace("\n", "\\N").strip()
    if entry.target_style in ("Main", "Top", "Narrator", "Narrator - Top"):
        return f"{{\\be2}}{text}"
    if entry.target_style in ("OP-Ara", "OP-Rom"):
        return f"{{\\fad(150,150)\\be2}}{text}"
    if entry.target_style in ("ED-Ara", "ED-Rom"):
        return f"{{\\fad(100,100)\\be2}}{text}"
    if entry.target_style == "Signs":
        tags = entry.preserved_tags
        if "title" in entry.actor.lower() or "episode" in entry.actor.lower() or "\\fs150" in tags:
            font_tag = "\\fnAFSANEH_v2 KHWF\\blur2"
        elif any(k in text for k in ("مملكة", "قصر", "قرية", "مدينة", "غابة", "قاعدة")):
            font_tag = "\\fnBKR Shahd Extra Bold\\blur2"
        else:
            font_tag = "\\fnBahij Mitra\\bord1.5\\blur1.5"
        return f"{{{font_tag}{tags}}}{text}"
    return text


def write_styled_ass(
    entries: List[SubEntry],
    translations: Dict[int, str],
    out_ass_path: str,
    credits: Optional[Dict[str, str]] = None,
) -> str:
    lines = [load_ass_header()]
    for entry in entries:
        final_text = entry.plain_text if entry.keep_untranslated else translations.get(entry.index, entry.plain_text)
        lines.append(
            f"Dialogue: 0,{_ms_to_ass_time(entry.start_ms)},{_ms_to_ass_time(entry.end_ms)},"
            f"{entry.target_style},{entry.actor},0,0,0,,{_format_event_text(entry, final_text)}"
        )

    if credits:
        slots = [
            ("translator", "ترجمة", "0:00:05.00", "0:00:09.00", "465,806"),
            ("typesetter", "محاكاة", "0:00:09.20", "0:00:13.00", "1100,392"),
            ("encoder", "دمج ورفع", "0:00:13.20", "0:00:17.50", "468,606"),
        ]
        for key, ar_label, st, en, pos in slots:
            val = (credits.get(key) or "").strip()
            if val:
                txt = (
                    f"{{\\fad(250,250)\\fnLoving Stay\\b1\\fs55\\c&HFFFFFF&\\bord0.5\\be2\\3c&H000000&"
                    f"\\pos({pos})}}{val}{{\\fnHayah\\fs80\\b0}} :{ar_label}"
                )
                lines.append(f"Dialogue: 1,{st},{en},Rights,,0,0,0,,{txt}")

    with open(out_ass_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    return out_ass_path
