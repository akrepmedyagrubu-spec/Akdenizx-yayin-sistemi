from __future__ import annotations

import http.server
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

WIDTH, HEIGHT = 854, 480
FPS = 24
SEGMENT_SECONDS = int(os.getenv("HLS_SEGMENT_SECONDS", "4"))
LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "12"))


def load_playlist() -> list[str]:
    if not PLAYLIST_FILE.is_file():
        raise RuntimeError(f"playlist.txt bulunamadı: {PLAYLIST_FILE}")

    entries = []
    lines = PLAYLIST_FILE.read_text(encoding="utf-8-sig").splitlines()

    for line_number, raw in enumerate(lines, 1):
        item = raw.strip()
        if not item or item.startswith("#"):
            continue

        path = pathlib.Path(item)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(
                f"playlist.txt:{line_number}: "
                "Yalnızca media/ içindeki dosya adı yazılabilir"
            )

        video = (VIDEO_DIR / path).resolve()
        if not video.is_relative_to(VIDEO_DIR) or not video.is_file():
            raise RuntimeError(
                f"playlist.txt:{line_number}: Video bulunamadı: {item}"
            )

        entries.append(item)

    if not entries:
        raise RuntimeError("playlist.txt içinde yayınlanacak video yok")

    return entries


def publish_loop() -> None:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            playlist = load_playlist()

            for item in playlist:
                video = (VIDEO_DIR / item).resolve()
                logo_name = (
                    "reklam.png"
                    if pathlib.Path(item).name.lower() == "reklam.mp4"
                    else "yayin.png"
                )
                logo = (VIDEO_DIR / logo_name).resolve()

                if not logo.is_relative_to(VIDEO_DIR) or not logo.is_file():
                    LOG.error(
                        "Logo bulunamadı: media/%s; video atlanıyor",
                        logo_name,
                    )
                    continue

                segment_pattern = OUTPUT_DIR / "segment_%06d.ts"

                command = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "warning",
                    "-nostdin",
                    "-y",
                    "-i", str(video),
                    "-loop", "1",
                    "-framerate", str(FPS),
                    "-i", str(logo),
                    "-filter_complex",
                    (
                        f"[0:v]scale={WIDTH}:{HEIGHT}:flags=bilinear,"
                        f"setsar=1[base];"
                        f"[1:v]scale={WIDTH}:{HEIGHT}:flags=bilinear,"
                        f"setsar=1[mark];"
                        "[base][mark]overlay=0:0:shortest=1,"
                        "format=yuv420p[outv]"
                    ),
                    "-map", "[outv]",
                    "-map", "0:a?",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-threads", "2",
                    "-b:v", "900k",
                    "-maxrate", "1000k",
                    "-bufsize", "1800k",
                    "-r", str(FPS),
                    "-g", str(FPS * 2),
                    "-sc_threshold", "0",
                    "-c:a", "aac",
                    "-b:a", "96k",
                    "-ar", "44100",
                    "-ac", "2",
                    "-f", "hls",
                    "-hls_time", str(SEGMENT_SECONDS),
                    "-hls_list_size", str(LIST_SIZE),
                    "-hls_flags",
                    (
                        "append_list+delete_segments+omit_endlist+"
                        "independent_segments+temp_file"
                    ),
                    "-hls_segment_filename", str(segment_pattern),
                    str(OUTPUT_DIR / "stream.m3u8"),
                ]

                LOG.info("Yayınlanıyor: %s (overlay: %s)", item, logo_name)
                result = subprocess.run(command, check=False)

                if result.returncode:
                    LOG.error(
                        "FFmpeg %s için %s koduyla durdu",
                        item,
                        result.returncode,
                    )
                else:
                    LOG.info("Tamamlandı: %s", item)

        except Exception:
            LOG.exception("Yayın döngüsünde hata; 5 saniye sonra tekrar denenecek")
            time.sleep(5)


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
    LOG.info(
        "HLS HTTP sunucusu :%s üzerinde hazır; adres /stream.m3u8",
        PORT,
    )
    server.serve_forever()
