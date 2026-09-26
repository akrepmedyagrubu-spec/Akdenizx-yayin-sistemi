from __future__ import annotations

import http.server
import json
import logging
import os
import pathlib
import subprocess
import threading
import time
import urllib.parse


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("hls-publisher")

ROOT = pathlib.Path(__file__).resolve().parent
VIDEO_DIR = pathlib.Path(os.getenv("VIDEO_DIR", ROOT / "media")).resolve()
OUTPUT_DIR = pathlib.Path(os.getenv("OUTPUT_DIR", ROOT / "public")).resolve()
PLAYLIST_FILE = pathlib.Path(os.getenv("PLAYLIST_FILE", ROOT / "playlist.txt")).resolve()
PORT = int(os.getenv("PORT", "10000"))

WIDTH = 854
HEIGHT = 480
FPS = 24
SEGMENT_SECONDS = 2
LIST_SIZE = 12
CONCAT_FILE = pathlib.Path("/tmp/hls_playlist.ffconcat")


def load_playlist() -> list[tuple[str, pathlib.Path, float]]:
    if not PLAYLIST_FILE.is_file():
        raise RuntimeError(f"playlist.txt bulunamadı: {PLAYLIST_FILE}")

    entries = []

    for line_number, raw in enumerate(
        PLAYLIST_FILE.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        item = raw.strip()

        if not item or item.startswith("#"):
            continue

        relative_path = pathlib.Path(item)

        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(
                f"playlist.txt:{line_number}: "
                "Yalnızca media/ içindeki göreli dosya adlarını kullan"
            )

        video = (VIDEO_DIR / relative_path).resolve()

        if not video.is_relative_to(VIDEO_DIR) or not video.is_file():
            raise RuntimeError(
                f"playlist.txt:{line_number}: Video bulunamadı: {item}"
            )

        probe = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
                "-of", "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        info = json.loads(probe.stdout)
        duration = float(info["format"]["duration"])
        entries.append((item, video, duration))

    if not entries:
        raise RuntimeError("playlist.txt içinde yayınlanacak video yok")

    return entries


def quoted_concat_path(path: pathlib.Path) -> str:
    # FFmpeg concat-list söz diziminde tek tırnakları kaçır.
    return str(path).replace("'", "'\\''")


def check_compatible_media(entries: list[tuple[str, pathlib.Path, float]]) -> None:
    reference = None

    for item, video, _duration in entries:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
                "-of", "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        signature = tuple(
            (stream.get("codec_type"), stream.get("codec_name"),
             stream.get("sample_rate"), stream.get("channels"))
            for stream in streams
            if stream.get("codec_type") in ("video", "audio")
        )

        if reference is None:
            reference = signature
        elif signature != reference:
            raise RuntimeError(
                "Tek FFmpeg akışı için videoların ses/video akışları aynı "
                "codec ve ses kanal yapısında olmalı. "
                f"Uyumsuz video: {item}"
            )


def build_concat_file(entries: list[tuple[str, pathlib.Path, float]]) -> None:
    lines = ["ffconcat version 1.0"]

    for _item, video, _duration in entries:
        lines.append(f"file '{quoted_concat_path(video)}'")

    CONCAT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_ad_enable_expression(
    entries: list[tuple[str, pathlib.Path, float]]
) -> str:
    cycle_duration = sum(duration for _item, _video, duration in entries)
    if cycle_duration <= 0:
        raise RuntimeError("Playlist süresi okunamadı")

    intervals = []
    position = 0.0

    for item, _video, duration in entries:
        if pathlib.Path(item).name.lower() == "reklam.mp4":
            end = position + duration
            intervals.append(
                "gte(mod(t\\,{cycle})\\,{start})*"
                "lt(mod(t\\,{cycle})\\,{end})".format(
                    cycle=f"{cycle_duration:.6f}",
                    start=f"{position:.6f}",
                    end=f"{end:.6f}",
                )
            )
        position += duration

    return "+".join(intervals) if intervals else "0"


def ffmpeg_command(entries: list[tuple[str, pathlib.Path, float]]) -> list[str]:
    regular_logo = VIDEO_DIR / "yayin.png"
    ad_logo = VIDEO_DIR / "reklam.png"

    if not regular_logo.is_file():
        raise RuntimeError("media/yayin.png bulunamadı")
    if not ad_logo.is_file():
        raise RuntimeError("media/reklam.png bulunamadı")

    build_concat_file(entries)
    ad_enable = make_ad_enable_expression(entries)

    filter_graph = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:flags=bilinear,setsar=1[base];"
        f"[1:v]scale={WIDTH}:{HEIGHT}:flags=bilinear[normal];"
        f"[2:v]scale={WIDTH}:{HEIGHT}:flags=bilinear[ad];"
        "[base][normal]overlay=0:0[with_normal];"
        f"[with_normal][ad]overlay=0:0:enable='{ad_enable}',"
        "format=yuv420p[outv]"
    )

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",

        # Tek concat girdisi; tüm playlist sonsuz döngüde ve gerçek zamanda.
        "-re",
        "-stream_loop", "-1",
        "-f", "concat",
        "-safe", "0",
        "-i", str(CONCAT_FILE),

        # Logolar sabit kare olarak döner; playlist videosunun üstüne basılır.
        "-loop", "1",
        "-framerate", str(FPS),
        "-i", str(regular_logo),
        "-loop", "1",
        "-framerate", str(FPS),
        "-i", str(ad_logo),

        "-filter_complex", filter_graph,
        "-map", "[outv]",
        "-map", "0:a?",

        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "2",
        "-b:v", "900k",
        "-maxrate", "1000k",
        "-bufsize", "1800k",
        "-r", str(FPS),
        "-g", str(FPS * SEGMENT_SECONDS),
        "-sc_threshold", "0",
        "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",

        "-c:a", "aac",
        "-b:a", "96k",
        "-ar", "44100",
        "-ac", "2",

        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", str(LIST_SIZE),
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename", str(OUTPUT_DIR / "segment_%08d.ts"),
        str(OUTPUT_DIR / "stream.m3u8"),
    ]


def publisher_loop() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            entries = load_playlist()
            check_compatible_media(entries)
            command = ffmpeg_command(entries)

            LOG.info(
                "Tek FFmpeg süreci başlıyor; %s video playlist sırasıyla dönecek",
                len(entries),
            )
            result = subprocess.run(command, check=False)

            LOG.error("FFmpeg durdu (%s); 5 saniye sonra yeniden başlatılıyor", result.returncode)
            time.sleep(5)

        except Exception:
            LOG.exception("Yayın başlatılamadı; 10 saniye sonra tekrar denenecek")
            time.sleep(10)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OUTPUT_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate, max-age=0",
        )
        super().end_headers()

    def do_GET(self):
        request_path = urllib.parse.urlparse(self.path).path

        if request_path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if request_path == "/":
            self.send_response(302)
            self.send_header("Location", "/stream.m3u8")
            self.end_headers()
            return

        super().do_GET()

    def log_message(self, fmt, *args):
        LOG.info("HTTP %s", fmt % args)


if __name__ == "__main__":
    threading.Thread(
        target=publisher_loop,
        name="ffmpeg-publisher",
        daemon=True,
    ).start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("HTTP sunucusu hazır: /stream.m3u8")
    server.serve_forever()
