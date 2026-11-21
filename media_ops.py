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


ProgressCallback = Callable[[float, str, str], None]



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


_EP_NUM_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})"),
    re.compile(r"(?:^|[\s_\-\[\(])(?:ep|episode|e)[\s_.\-]*(\d{1,4})(?:v\d+)?(?:[\s_\-\]\)\.\[]|$)", re.IGNORECASE),
    re.compile(r"-\s*(\d{1,4})(?:v\d+)?\s*[\[\.\-]"),
    re.compile(r"(?:^|[\s_\-\[\(])(\d{2,4})(?:v\d+)?(?:[\s_\-\]\)\.\[]|$)"),
]

_RESOLUTION_NUMBERS = {360, 480, 540, 576, 720, 900, 1080, 1440, 2160}


def extract_episode_number(name: str) -> Optional[int]:
    """Best-effort episode number from a fansub filename (S01E05, 'Name - 05', 'Ep 05'...)."""
    stem = Path(name).stem
    m = _EP_NUM_PATTERNS[0].search(stem)
    if m:
        return int(m.group(2))
    for pat in _EP_NUM_PATTERNS[1:]:
        for mm in pat.finditer(stem):
            num = int(mm.group(1))
            if num in _RESOLUTION_NUMBERS:
                continue
            return num
    return None


def episode_sort_key(path) -> Tuple[int, str]:
    p = Path(path)
    num = extract_episode_number(p.name)
    return (num if num is not None else 99999, p.name.lower())


def collect_episode_videos(folder) -> List[Path]:
    """All fully downloaded videos inside a season folder, sorted in episode order."""
    folder = Path(folder)
    if not folder.exists():
        return []
    if any(folder.rglob("*.aria2")):
        return []
    vids = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and p.stat().st_size > 1024 * 1024
    ]
    vids.sort(key=episode_sort_key)
    return vids


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

    _run_aria2(target, output_dir, log_cb=log_cb, progress_cb=progress_cb)

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


def _load_bt_trackers(log_cb: Optional[Callable[[str], None]] = None) -> str:
    """Best-effort public tracker list for magnet/torrent swarms.

    Order: fresh pod cache (/workspace/trackers.txt, 7 days) -> live fetch from
    ngosang/trackerslist -> list shipped in the repo (trackers.txt). Returns a
    comma-joined tracker string for aria2's --bt-tracker, or "" on total failure.
    """
    cache = Path("/workspace/trackers.txt")
    try:
        if cache.is_file() and (time.time() - cache.stat().st_mtime) < 7 * 86400:
            txt = cache.read_text(encoding="utf-8").strip()
            if txt:
                return ",".join(txt.split())
    except OSError:
        pass
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
            headers={"User-Agent": "runpod-anime-subber"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            txt = resp.read().decode("utf-8", "replace").strip()
        if txt:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(txt, encoding="utf-8")
            except OSError:
                pass
            if log_cb:
                log_cb(f"🧲 تم تحديث قائمة الـ trackers ({len(txt.split())} متتبعاً).")
            return ",".join(txt.split())
    except Exception as exc:
        if log_cb:
            log_cb(f"ℹ️ تعذر تحديث قائمة الـ trackers ({exc}) — استخدام النسخة المرفقة.")
    repo_list = Path(__file__).resolve().parent / "trackers.txt"
    try:
        if repo_list.is_file():
            txt = repo_list.read_text(encoding="utf-8").strip()
            if txt:
                return ",".join(txt.split())
    except OSError:
        pass
    return ""


def _run_aria2(
    target: str,
    output_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> None:
    """Run aria2c (torrent/magnet/URL) into output_dir with resume support and live progress."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
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
    bt_trackers = _load_bt_trackers(log_cb=log_cb)
    if bt_trackers:
        cmd[-1:-1] = [
            f"--bt-tracker={bt_trackers}",
            "--bt-tracker-connect-timeout=10",
            "--bt-tracker-timeout=10",
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


def download_season_batch(
    source_link: Optional[str],
    torrent_file: Optional[str],
    output_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> List[Path]:
    """Download a whole season/batch torrent and return every episode video in episode order.

    A link may contain one episode or several seasons; all videos found are returned,
    sorted by the episode number extracted from each filename. Reuses the completed
    download on later runs (resume-safe).
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    cached = collect_episode_videos(out_path)
    if cached:
        total_mb = sum(p.stat().st_size for p in cached) / (1024 * 1024)
        if log_cb:
            log_cb(f"⚡ الموسم محفوظ مسبقاً بالكامل ({len(cached)} ملف فيديو، {total_mb:.0f} MB) — تم تخطي التحميل!")
        if progress_cb:
            progress_cb(100.0, "محفوظ مسبقاً على السيرفر ⚡", f"{len(cached)} حلقة · {total_mb:.0f} MB")
        return cached

    target = None
    if torrent_file and os.path.isfile(torrent_file):
        target = torrent_file
    elif source_link and source_link.strip():
        target = source_link.strip()
    if not target:
        raise ValueError("لم يتم إدخال رابط تورنت للموسم.")

    _run_aria2(target, output_dir, log_cb=log_cb, progress_cb=progress_cb)

    vids = collect_episode_videos(out_path)
    if not vids:
        raise FileNotFoundError(f"لم يتم العثور على ملفات فيديو داخل {output_dir}")
    if log_cb:
        log_cb(f"✓ اكتمل تحميل الدفعة: {len(vids)} حلقة بالترتيب:")
        for v in vids:
            ep = extract_episode_number(v.name)
            log_cb(f"   • حلقة {ep if ep is not None else '?'} ← {v.name}")
    if progress_cb:
        progress_cb(100.0, "اكتمل تحميل الدفعة 100% ✓", f"{len(vids)} حلقة")
    return vids


def _escape_ffmpeg_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


_NVENC_CAPS: Dict[str, set] = {}


def _has_nvenc(codec: str) -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return codec in (res.stdout or "")


def _nvenc_opts(codec: str) -> set:
    """Return the set of AVOptions this ffmpeg build's NVENC encoder supports (for safe flag gating)."""
    if codec not in _NVENC_CAPS:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", f"encoder={codec}"],
            capture_output=True, text=True,
        )
        _NVENC_CAPS[codec] = set((res.stdout or "").split())
    return _NVENC_CAPS[codec]


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
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """Single-pass NVENC burn: responsive ASS subtitles burned + compressed to MP4."""
    verify_required_fonts(fonts_dir)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ass_filter = f"ass='{_escape_ffmpeg_path(os.path.abspath(ass_path))}':fontsdir='{_escape_ffmpeg_path(os.path.abspath(fonts_dir))}'"

    if codec.endswith("_nvenc") and _has_nvenc(codec):
        v_args = [
            "-c:v", codec, "-preset", preset, "-rc", "vbr",
            "-cq", str(cq), "-b:v", "0",
            "-spatial-aq", "1", "-temporal-aq", "1", "-pix_fmt", "yuv420p",
        ]
        caps = _nvenc_opts(codec)
        if "-multipass" in caps:
            v_args += ["-multipass", "fullres"]  # better quality at the same size
        if "-bf" in caps:
            v_args += ["-bf", "3"]  # B-frames shrink anime encodes noticeably
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

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", ass_filter, *v_args,
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        output_path,
    ]

    if log_cb:
        log_cb(f"[ffmpeg] بدء الحرق والضغط لـ {Path(output_path).name} ({v_args[1]})...")
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


def qc_burned_episode(
    source_path: str,
    output_path: str,
    ass_path: str = "",
    expected_lines: int = 0,
    qc_dir: str = "",
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Post-burn QC: output exists and is sizable, duration matches source,
    3 sample frames extracted for review, ASS dialogue count vs translation count."""
    def log(m: str) -> None:
        if log_cb:
            log_cb(m)

    report: Dict[str, object] = {"ok": True, "issues": []}

    def issue(msg: str) -> None:
        report["ok"] = False
        report["issues"].append(msg)  # type: ignore[attr-defined]
        log(f"⚠️ QC: {msg}")

    out = Path(output_path)
    name = out.name
    if not out.is_file() or out.stat().st_size < 256 * 1024:
        issue(f"الملف الناتج مفقود أو صغير بشكل غير طبيعي: {name}")

    size_mb = out.stat().st_size / (1024 * 1024) if out.is_file() else 0.0
    src_dur = out_dur = 0.0
    try:
        _, _, _, src_dur = probe_video_info(source_path)
        _, _, _, out_dur = probe_video_info(output_path)
        report["src_dur"] = src_dur
        report["out_dur"] = out_dur
        if src_dur > 0 and out_dur > 0 and abs(src_dur - out_dur) > max(2.0, src_dur * 0.01):
            issue(f"مدة الناتج ({out_dur:.1f}s) مختلفة عن الأصل ({src_dur:.1f}s)")
    except Exception as exc:
        log(f"⚠️ QC: تعذر فحص المدة ({exc})")

    frames_ok = 0
    if qc_dir and out_dur > 0:
        try:
            Path(qc_dir).mkdir(parents=True, exist_ok=True)
            for i, frac in enumerate((0.1, 0.5, 0.9), 1):
                jpg = Path(qc_dir) / f"{out.stem}_qc{i}.jpg"
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{out_dur * frac:.1f}",
                     "-i", str(out), "-frames:v", "1", "-q:v", "4", str(jpg)],
                    capture_output=True, timeout=90,
                )
                if jpg.is_file() and jpg.stat().st_size > 4096:
                    frames_ok += 1
            if frames_ok < 3:
                issue(f"تم استخراج {frames_ok}/3 لقطات معاينة فقط")
        except Exception as exc:
            log(f"⚠️ QC: تعذر استخراج لقطات المعاينة ({exc})")

    n_dlg = 0
    if ass_path and os.path.isfile(ass_path) and expected_lines:
        try:
            with open(ass_path, encoding="utf-8", errors="ignore") as fh:
                n_dlg = sum(1 for ln in fh if ln.startswith("Dialogue:"))
            report["ass_lines"] = n_dlg
            report["expected_lines"] = expected_lines
            if abs(n_dlg - expected_lines) > max(2, int(expected_lines * 0.02)):
                issue(f"أسطر الحوار في ASS ({n_dlg}) مختلفة عن الأسطر المترجمة ({expected_lines})")
        except Exception as exc:
            log(f"⚠️ QC: تعذر عد أسطر ASS ({exc})")

    report["size_mb"] = round(size_mb, 1)
    report["frames"] = frames_ok
    if report["ok"]:
        log(
            f"✅ QC سليم لـ {name}: المدة {out_dur:.0f}s"
            + (f" (الأصل {src_dur:.0f}s)" if src_dur else "")
            + f" · الحجم {size_mb:.0f}MB"
            + (f" · الأسطر {n_dlg}/{expected_lines}" if expected_lines else "")
            + (f" · {frames_ok} لقطات معاينة" if frames_ok else "")
        )
    else:
        log(f"⚠️ QC لـ {name}: {len(report['issues'])} ملاحظة — راجع السجل")
    return report
