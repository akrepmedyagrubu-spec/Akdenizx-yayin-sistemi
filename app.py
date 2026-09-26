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
PLAYLIST_FILE = pathlib.Path(
    os.getenv("PLAYLIST_FILE", ROOT / "playlist.txt")
).resolve()
CONCAT_FILE = pathlib.Path("/tmp/hls_playlist.ffconcat")
PORT = int(os.getenv("PORT", "10000"))

WIDTH = 854
HEIGHT = 480
FPS = 24
SEGMENT_SECONDS = 2
LIST_SIZE = 12


def probe_video(video: pathlib.Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,sample_rate,channels",
            "-of", "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def load_playlist() -> list[tuple[str, pathlib.Path, float, tuple]]:
    if not PLAYLIST_FILE.is_file():
        raise RuntimeError(f"playlist.txt bulunamadı: {PLAYLIST_FILE}")

    entries = []
    reference_streams = None

    for line_number, raw in enumerate(
        PLAYLIST_FILE.read_text(encoding="utf-8-sig").splitlines(),
        1,
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

        info = probe_video(video)
        duration = float(info["format"]["duration"])
        streams = tuple(
            (
                stream.get("codec_type"),
                stream.get("codec_name"),
                stream.get("sample_rate"),
                stream.get("channels"),
            )
            for stream in info.get("streams", [])
            if stream.get("codec_type") in ("video", "audio")
        )

        if reference_streams is None:
            reference_streams = streams
        elif streams != reference_streams:
            raise RuntimeError(
                "Tek FFmpeg akışı için playlist'teki videoların ses/video "
                "codec yapısı aynı olmalı. Uyumsuz dosya: " + item
            )

        entries.append((item, video, duration, streams))

    if not entries:
        raise RuntimeError("playlist.txt içinde yayınlanacak video yok")

    return entries


def ffconcat_escape(path: pathlib.Path) -> str:
    return str(path).replace("'", "'\\''")


def make_ad_expression(entries: list[tuple]) -> str:
    total_duration = sum(entry[2] for entry in entries)
    if total_duration <= 0:
        raise RuntimeError("Playlist süresi okunamadı")

    ad_ranges = []
    position = 0.0

    for item, _video, duration, _streams in entries:
        if pathlib.Path(item).name.lower() == "reklam.mp4":
            end = position + duration
            ad_ranges.append(
                "gte(mod(t\\,{total})\\,{start})*"
                "lt(mod(t\\,{total})\\,{end})".format(
                    total=f"{total_duration:.6f}",
                    start=f"{position:.6f}",
                    end=f"{end:.6f}",
                )
            )
        position += duration

    return "+".join(ad_ranges) if ad_ranges else "0"


def build_ffmpeg_command(entries: list[tuple]) -> list[str]:
    regular_logo = VIDEO_DIR / "yayin.png"
    ad_logo = VIDEO_DIR / "reklam.png"

    if not regular_logo.is_file():
        raise RuntimeError("media/yayin.png bulunamadı")
    if not ad_logo.is_file():
        raise RuntimeError("media/reklam.png bulunamadı")

    CONCAT_FILE.write_text(
        "ffconcat version 1.0\n"
        + "".join(
            f"file '{ffconcat_escape(video)}'\n"
            for _item, video, _duration, _streams in entries
        ),
        encoding="utf-8",
    )

    ad_expression = make_ad_expression(entries)

    # Normal logo reklam aralığında kapalıdır.
    # Reklam görseli yalnızca reklam.mp4 süresince açıktır.
    filter_graph = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:flags=bilinear,setsar=1[base];"
        f"[1:v]scale={WIDTH}:{HEIGHT}:flags=bilinear[normal];"
        f"[2:v]scale={WIDTH}:{HEIGHT}:flags=bilinear[ad];"
        f"[base][normal]overlay=0:0:"
        f"enable='not({ad_expression})'[with_logo];"
        f"[with_logo][ad]overlay=0:0:"
        f"enable='{ad_expression}',format=yuv420p[outv]"
    )

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",

        # Tek FFmpeg girdisi tüm listeyi sırayla sonsuz döndürür.
        "-re",
        "-stream_loop", "-1",
        "-f", "concat",
        "-safe", "0",
        "-i", str(CONCAT_FILE),

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
        "-force_key_frames",
        f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",

        "-c:a", "aac",
        "-b:a", "96k",
        "-ar", "44100",
        "-ac", "2",

        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", str(LIST_SIZE),
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments+temp_file",
        "-hls_segment_filename",
        str(OUTPUT_DIR / "segment_%08d.ts"),
        str(OUTPUT_DIR / "stream.m3u8"),
    ]


def publish_loop() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            entries = load_playlist()
            command = build_ffmpeg_command(entries)

            LOG.info(
                "Tek FFmpeg süreci başladı; %d video sırayla yayınlanacak",
                len(entries),
            )
            result = subprocess.run(command, check=False)

            LOG.error(
                "FFmpeg durdu (kod %s); 5 saniye sonra yeniden denenecek",
                result.returncode,
            )
            time.sleep(5)

        except Exception:
            LOG.exception(
                "Yayın başlatılamadı; 10 saniye sonra tekrar denenecek"
            )
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
        target=publish_loop,
        name="ffmpeg-publisher",
        daemon=True,
    ).start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("HTTP sunucusu hazır: /stream.m3u8")
    server.serve_forever()
