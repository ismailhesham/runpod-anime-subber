import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

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


def verify_required_fonts(fonts_dir: str) -> None:
    """Strictly verify all 7 custom fonts exist in fonts/ so libass never substitutes random fonts."""
    fdir = Path(fonts_dir)
    missing = [f for f in REQUIRED_FONT_FILES if not (fdir / f).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing required font file(s) in {fdir}: {missing}. "
            "Make sure the fonts/ folder was uploaded to GitHub and cloned completely."
        )


def download_torrent_or_url(
    source_link: Optional[str],
    torrent_file: Optional[str],
    output_dir: str,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Path:
    """Download a single quality (magnet link, URL, or .torrent/.mkv file) via aria2c."""
    os.makedirs(output_dir, exist_ok=True)

    target = None
    if torrent_file and os.path.isfile(torrent_file):
        if Path(torrent_file).suffix.lower() in VIDEO_EXTENSIONS:
            return Path(torrent_file)
        target = torrent_file
    elif source_link and source_link.strip():
        target = source_link.strip()

    if not target:
        raise ValueError("No download link or file provided.")

    cmd = [
        "aria2c",
        "--dir", output_dir,
        "--seed-time=0",
        "--bt-stop-timeout=300",
        "--max-connection-per-server=8",
        "--split=8",
        "--file-allocation=none",
        "--summary-interval=5",
        "--allow-overwrite=true",
        target,
    ]
    if log_cb:
        log_cb(f"[aria2c] Downloading into {output_dir} (--seed-time=0)...")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        clean = line.strip()
        if clean and log_cb and any(k in clean for k in ("DL:", "Download complete", "ERROR", "%")):
            log_cb(f"[aria2c] {clean}")

    if proc.wait() != 0:
        raise RuntimeError("aria2c download failed. Check the magnet/link or Runpod P2P ports.")

    videos = sorted(
        [p for p in Path(output_dir).rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not videos:
        raise FileNotFoundError(f"No video file found in {output_dir}")
    return videos[0]


def _escape_ffmpeg_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _has_nvenc(codec: str) -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return codec in (res.stdout or "")


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
) -> str:
    """Burn Arabic ASS subtitles + compress at the video's NATIVE resolution on NVIDIA NVENC."""
    verify_required_fonts(fonts_dir)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    vf = f"ass='{_escape_ffmpeg_path(os.path.abspath(ass_path))}':fontsdir='{_escape_ffmpeg_path(os.path.abspath(fonts_dir))}'"

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

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", vf, *v_args,
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        output_path,
    ]
    if log_cb:
        log_cb(f"[ffmpeg] Encoding {Path(output_path).name} ({v_args[1]}, CQ={cq})...")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    tail: List[str] = []
    for line in proc.stdout:
        clean = line.strip()
        if not clean:
            continue
        tail.append(clean)
        if len(tail) > 20:
            tail.pop(0)
        if log_cb and "frame=" in clean and "speed=" in clean:
            log_cb(f"[ffmpeg] {clean}")

    if proc.wait() != 0:
        raise RuntimeError("FFmpeg failed:\n" + "\n".join(tail[-8:]))

    orig_mb = os.path.getsize(video_path) / (1024 * 1024)
    new_mb = os.path.getsize(output_path) / (1024 * 1024)
    if log_cb:
        log_cb(f"[ffmpeg] Done: {orig_mb:.1f} MB -> {new_mb:.1f} MB ({output_path})")
    return output_path


def upload_to_rclone(
    local_file: str,
    remote_name: str,
    folder_path: str = "Runpod_Anime",
    generate_link: bool = True,
    log_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """Upload file via Rclone (MEGA or Google Drive) and optionally return public share link."""
    fname = os.path.basename(local_file)
    dest_dir = f"{remote_name.strip().rstrip(':')}:{folder_path.strip().strip('/')}"
    dest_file = f"{dest_dir}/{fname}"

    proc = subprocess.Popen(
        ["rclone", "copy", local_file, dest_dir, "--stats=5s", "--stats-one-line", "-v"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    tail = []
    for line in proc.stdout:
        clean = line.strip()
        if clean:
            tail.append(clean)
            if len(tail) > 12:
                tail.pop(0)
            if log_cb and ("%" in clean or "Transferred" in clean or "ERROR" in clean):
                log_cb(f"[rclone:{remote_name}] {clean}")

    if proc.wait() != 0:
        raise RuntimeError(f"Rclone upload to {remote_name} failed:\n" + "\n".join(tail[-6:]))

    if generate_link:
        link_res = subprocess.run(["rclone", "link", dest_file], capture_output=True, text=True)
        if link_res.returncode == 0 and link_res.stdout.strip():
            return link_res.stdout.strip()
    return dest_file


def upload_to_doodstream(
    local_file: str,
    api_key: str,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """Upload video to DoodStream via official REST API using streaming curl."""
    key = api_key.strip()
    if not key:
        raise ValueError("Please enter your DoodStream API Key.")

    req = urllib.request.Request(f"https://doodapi.co/api/upload/server?key={key}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        server_data = json.loads(resp.read().decode("utf-8"))

    if server_data.get("status") != 200 or not server_data.get("result"):
        raise RuntimeError(f"DoodStream server error: {server_data}")

    upload_url = f"{server_data['result']}?{key}"
    if log_cb:
        log_cb(f"[DoodStream] Streaming {os.path.basename(local_file)}...")

    res = subprocess.run(
        ["curl", "-sS", "-X", "POST", "-F", f"api_key={key}", "-F", f"file=@{local_file}", upload_url],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"DoodStream curl error: {res.stderr[-300:]}")

    data = json.loads(res.stdout or "{}")
    if data.get("status") != 200 or not data.get("result"):
        raise RuntimeError(f"DoodStream API error: {data}")

    item = data["result"][0] if isinstance(data["result"], list) else data["result"]
    code = item.get("filecode", "")
    return {
        "filecode": code,
        "watch_url": item.get("download_url") or f"https://doodstream.com/d/{code}",
        "embed_url": item.get("protected_embed") or f"https://doodstream.com/e/{code}",
    }
