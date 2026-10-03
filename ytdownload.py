#!/usr/bin/env python3
"""
yt_channel_downloader.py

Tải 12 video mới nhất của mỗi kênh YouTube bằng yt-dlp.

Chức năng:
- Chỉ tải video dài 6 - 40 phút
- Độ phân giải tối đa 1080p
- Nếu không có 1080p thì lấy bản cao nhất <= 1080p
- Tự động ghép video + audio bằng FFmpeg
- Tự động tìm ffmpeg.exe / ffprobe.exe trong thư mục chứa script
- Lưu tên video vào file tổng
- Video trùng tên sẽ được bỏ qua
- Tải song song nhiều video
- Tải song song nhiều fragment
- Hỗ trợ aria2c nếu có

Cấu trúc thư mục đề nghị:

    folder/
    │
    ├── yt_channel_downloader.py
    ├── ffmpeg.exe
    ├── ffprobe.exe
    ├── aria2c.exe             (tuỳ chọn)
    └── channels.txt           (tự tạo nếu chưa có)

Cài yt-dlp:

    pip install -U yt-dlp

Chạy:

    python yt_channel_downloader.py

Hoặc:

    python yt_channel_downloader.py -c my_channels.txt -o D:\\videos -w 4

Hoặc truyền trực tiếp channel:

    python yt_channel_downloader.py https://www.youtube.com/@abc
"""

import argparse
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from yt_dlp import YoutubeDL


# ==========================================================
# CẤU HÌNH
# ==========================================================

VIDEOS_PER_CHANNEL = 12

MIN_DURATION = 6 * 60       # 6 phút
MAX_DURATION = 40 * 60      # 40 phút

MAX_HEIGHT = 1080

PARALLEL_VIDEOS = 3
CONCURRENT_FRAGMENTS = 16


# ==========================================================
# ĐƯỜNG DẪN
# ==========================================================

# Thư mục chứa file Python hiện tại
SCRIPT_DIR = Path(__file__).resolve().parent

# FFmpeg / FFprobe nằm cùng thư mục với script
FFMPEG_PATH = SCRIPT_DIR / "ffmpeg.exe"
FFPROBE_PATH = SCRIPT_DIR / "ffprobe.exe"

# aria2c cũng thử tìm trong thư mục script
ARIA2C_LOCAL_PATH = SCRIPT_DIR / "aria2c.exe"


# ==========================================================
# GLOBAL
# ==========================================================

lock = threading.Lock()

seen_titles: set[str] = set()


# ==========================================================
# HÀM LOG
# ==========================================================

def log(msg: str) -> None:
    with lock:
        print(msg, flush=True)


# ==========================================================
# CHUẨN HÓA TITLE
# ==========================================================

def norm(title: str) -> str:
    return " ".join(title.split()).casefold()


# ==========================================================
# LOAD DANH SÁCH TITLE ĐÃ DOWNLOAD
# ==========================================================

def load_titles(path: Path) -> set[str]:
    if not path.exists():
        return set()

    with path.open("r", encoding="utf-8") as f:
        return {
            norm(line)
            for line in f
            if line.strip()
        }


# ==========================================================
# SAVE TITLE
# ==========================================================

def save_title(path: Path, title: str) -> None:
    with lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(" ".join(title.split()) + "\n")


# ==========================================================
# TÌM FFmpeg
# ==========================================================

def find_ffmpeg() -> Path | None:
    """
    Ưu tiên tìm ffmpeg.exe trong thư mục chứa script.

    Nếu không có thì thử tìm trong PATH.
    """

    # 1. Cùng thư mục với script
    if FFMPEG_PATH.exists():
        return FFMPEG_PATH

    # 2. PATH
    path = shutil.which("ffmpeg")

    if path:
        return Path(path)

    return None


# ==========================================================
# TÌM FFprobe
# ==========================================================

def find_ffprobe() -> Path | None:
    """
    Ưu tiên tìm ffprobe.exe trong thư mục chứa script.

    Nếu không có thì thử tìm trong PATH.
    """

    # 1. Cùng thư mục với script
    if FFPROBE_PATH.exists():
        return FFPROBE_PATH

    # 2. PATH
    path = shutil.which("ffprobe")

    if path:
        return Path(path)

    return None


# ==========================================================
# TÌM ARIA2C
# ==========================================================

def find_aria2c() -> Path | None:
    """
    Tìm aria2c.exe:

    1. Cùng thư mục script
    2. PATH
    """

    if ARIA2C_LOCAL_PATH.exists():
        return ARIA2C_LOCAL_PATH

    path = shutil.which("aria2c")

    if path:
        return Path(path)

    return None


# ==========================================================
# CHANNEL URL
# ==========================================================

def channel_url(u: str) -> str:
    u = u.strip().rstrip("/")

    # @handle
    if u.startswith("@"):
        u = "https://www.youtube.com/" + u

    # Channel ID
    elif re.fullmatch(r"UC[\w-]{22}", u):
        u = "https://www.youtube.com/channel/" + u

    # Nếu đã là video / playlist
    if "watch?" in u or "playlist?" in u:
        return u

    # Nếu đã có tab
    if re.search(
        r"/(videos|streams|shorts|playlists|featured)$",
        u
    ):
        return u

    # Mặc định dùng tab Videos
    return u + "/videos"


# ==========================================================
# ĐỌC CHANNELS
# ==========================================================

def read_channels(args) -> list[str]:
    urls = list(args.channels)

    cfile = Path(args.channels_file)

    # Nếu không truyền URL trực tiếp
    if not urls:

        # Chưa có channels.txt
        if not cfile.exists():

            cfile.write_text(
                "# Mỗi dòng một kênh\n"
                "# Có thể dùng URL, @handle hoặc channel ID\n"
                "# Ví dụ:\n"
                "# https://www.youtube.com/@example\n"
                "# @example\n"
                "# UCxxxxxxxxxxxxxxxxxxxxxx\n",
                encoding="utf-8",
            )

            print(
                f"Đã tạo {cfile}. "
                "Hãy điền danh sách kênh rồi chạy lại."
            )

            sys.exit(0)

        # Đọc file
        for line in cfile.read_text(
            encoding="utf-8"
        ).splitlines():

            line = line.strip()

            if line and not line.startswith("#"):
                urls.append(line)

    return [
        channel_url(u)
        for u in urls
    ]


# ==========================================================
# LẤY VIDEO CỦA CHANNEL
# ==========================================================

def list_channel_videos(url: str) -> list[dict]:

    opts = {
        "quiet": True,
        "no_warnings": True,

        # Không download ở bước scan
        "skip_download": True,

        # Chỉ lấy thông tin dạng flat
        "extract_flat": True,

        # Chỉ lấy 12 video
        "playlistend": VIDEOS_PER_CHANNEL,

        "ignoreerrors": True,
    }

    with YoutubeDL(opts) as ydl:

        info = ydl.extract_info(
            url,
            download=False
        )

    if not info:
        return []

    entries = [
        e
        for e in (info.get("entries") or [])
        if e
    ]

    return entries[:VIDEOS_PER_CHANNEL]


# ==========================================================
# LẤY METADATA VIDEO
# ==========================================================

def video_meta(
    entry: dict
) -> tuple[str, str, int | None]:

    vid = entry.get("id")

    url = entry.get("url")

    if not url or not url.startswith("http"):
        url = (
            f"https://www.youtube.com/watch?v={vid}"
        )

    title = entry.get("title")

    duration = entry.get("duration")

    # Nếu thiếu title hoặc duration
    if title is None or duration is None:

        with YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }) as ydl:

            info = (
                ydl.extract_info(
                    url,
                    download=False
                )
                or {}
            )

        title = (
            title
            or info.get("title")
            or vid
        )

        duration = (
            duration
            if duration is not None
            else info.get("duration")
        )

    return url, title, duration


# ==========================================================
# BUILD OPTIONS CHO YT-DLP
# ==========================================================

def build_opts(
    outdir: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path | None,
    aria2c_path: Path | None,
) -> dict:

    opts = {

        # --------------------------------------------------
        # OUTPUT
        # --------------------------------------------------

        "outtmpl": str(
            outdir
            / "%(uploader)s"
            / "%(title)s.%(ext)s"
        ),

        # --------------------------------------------------
        # FORMAT
        # --------------------------------------------------

        # Video <= 1080p + audio tốt nhất
        #
        # Ưu tiên:
        #   video tốt nhất <=1080p
        #   +
        #   audio tốt nhất
        #
        # Nếu không có video/audio riêng:
        #   dùng format combined <=1080p
        #
        "format": (
            "bv*[height<=1080]+ba/"
            "b[height<=1080]"
        ),

        # Ưu tiên độ phân giải cao nhất <=1080
        "format_sort": [
            "res:1080",
            "ext:mp4:m4a",
        ],

        # --------------------------------------------------
        # MERGE
        # --------------------------------------------------

        # Ghép thành MP4
        "merge_output_format": "mp4",

        # FFmpeg
        "ffmpeg_location": str(ffmpeg_path),

        # --------------------------------------------------
        # WINDOWS
        # --------------------------------------------------

        "windowsfilenames": True,

        # --------------------------------------------------
        # LOG
        # --------------------------------------------------

        "quiet": True,
        "no_warnings": True,
        "noprogress": True,

        # --------------------------------------------------
        # RETRY
        # --------------------------------------------------

        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,

        # --------------------------------------------------
        # NETWORK
        # --------------------------------------------------

        "socket_timeout": 20,

        "buffersize": 1024 * 64,

        # --------------------------------------------------
        # FILE
        # --------------------------------------------------

        "overwrites": False,
        "continuedl": True,
    }


    # ======================================================
    # FFPROBE
    # ======================================================

    if ffprobe_path:

        # yt-dlp sử dụng ffmpeg_location để tìm cả ffprobe
        #
        # Không cần option riêng trong phần lớn trường hợp.
        pass


    # ======================================================
    # ARIA2C
    # ======================================================

    if aria2c_path:

        opts["external_downloader"] = {
            "default": str(aria2c_path)
        }

        opts["external_downloader_args"] = {

            "aria2c": [
                "-x16",
                "-s16",
                "-k1M",
                "--file-allocation=none",
                "--summary-interval=0",
            ]
        }

    else:

        # yt-dlp native fragment downloader
        opts["concurrent_fragment_downloads"] = (
            CONCURRENT_FRAGMENTS
        )

        # Chia HTTP request thành chunk
        opts["http_chunk_size"] = (
            10 * 1024 * 1024
        )


    return opts


# ==========================================================
# DOWNLOAD 1 VIDEO
# ==========================================================

def download_one(
    url: str,
    title: str,
    opts: dict,
    titles_file: Path,
) -> bool:

    try:

        log(f"[TẢI] {title}")

        with YoutubeDL(opts) as ydl:

            result = ydl.download([url])

        # Nếu yt-dlp báo thành công
        if result == 0:

            save_title(
                titles_file,
                title
            )

            log(f"[XONG] {title}")

            return True

        log(
            f"[LỖI] yt-dlp trả về mã {result}: {title}"
        )

        with lock:
            seen_titles.discard(norm(title))

        return False

    except Exception as e:

        with lock:
            seen_titles.discard(
                norm(title)
            )

        log(
            f"[LỖI] {title}: {e}"
        )

        return False


# ==========================================================
# MAIN
# ==========================================================

def main() -> None:

    # ------------------------------------------------------
    # ARGUMENTS
    # ------------------------------------------------------

    ap = argparse.ArgumentParser(
        description=(
            "Tải video mới nhất từ danh sách "
            "kênh YouTube"
        )
    )

    ap.add_argument(
        "channels",
        nargs="*",
        help=(
            "URL/@handle kênh "
            "(bỏ trống = đọc channels.txt)"
        ),
    )

    ap.add_argument(
        "-c",
        "--channels-file",
        default="channels.txt",
        help="file chứa danh sách kênh",
    )

    ap.add_argument(
        "-o",
        "--output",
        default="downloads",
        help="thư mục lưu video",
    )

    ap.add_argument(
        "-t",
        "--titles-file",
        default="downloaded_titles.txt",
        help="file lưu tên video đã tải",
    )

    ap.add_argument(
        "-w",
        "--workers",
        type=int,
        default=PARALLEL_VIDEOS,
        help="số video tải song song",
    )

    ap.add_argument(
        "--no-aria2c",
        action="store_true",
        help=(
            "không dùng aria2c "
            "dù đã cài"
        ),
    )

    args = ap.parse_args()


    # ------------------------------------------------------
    # TÌM FFMPEG
    # ------------------------------------------------------

    ffmpeg_path = find_ffmpeg()

    if not ffmpeg_path:

        print()
        print("=" * 70)
        print("LỖI: KHÔNG TÌM THẤY FFMPEG")
        print("=" * 70)
        print()
        print(
            "Script đã kiểm tra:"
        )
        print(
            f"  {FFMPEG_PATH}"
        )
        print()
        print(
            "Hãy đặt ffmpeg.exe cùng thư mục "
            "với file yt_channel_downloader.py"
        )
        print()
        print(
            "Hoặc thêm FFmpeg vào PATH."
        )
        print()
        sys.exit(1)


    # ------------------------------------------------------
    # TÌM FFPROBE
    # ------------------------------------------------------

    ffprobe_path = find_ffprobe()


    # ------------------------------------------------------
    # TÌM ARIA2C
    # ------------------------------------------------------

    aria2c_path = None

    if not args.no_aria2c:

        aria2c_path = find_aria2c()


    # ------------------------------------------------------
    # HIỂN THỊ THÔNG TIN
    # ------------------------------------------------------

    print()
    print("=" * 70)
    print("YouTube Channel Downloader")
    print("=" * 70)

    print(
        f"FFmpeg : {ffmpeg_path}"
    )

    if ffprobe_path:

        print(
            f"FFprobe: {ffprobe_path}"
        )

    else:

        print(
            "FFprobe: Không tìm thấy "
            "(không bắt buộc)"
        )


    if aria2c_path:

        print(
            f"aria2c : {aria2c_path}"
        )

    else:

        print(
            "aria2c : Không tìm thấy "
            "-> dùng native downloader"
        )

    print(
        f"Max resolution: {MAX_HEIGHT}p"
    )

    print(
        f"Video duration: "
        f"{MIN_DURATION // 60}-"
        f"{MAX_DURATION // 60} phút"
    )

    print()


    # ------------------------------------------------------
    # OUTPUT DIRECTORY
    # ------------------------------------------------------

    outdir = Path(
        args.output
    )

    outdir.mkdir(
        parents=True,
        exist_ok=True
    )


    # ------------------------------------------------------
    # TITLE FILE
    # ------------------------------------------------------

    titles_file = Path(
        args.titles_file
    )

    seen_titles.update(
        load_titles(titles_file)
    )


    # ------------------------------------------------------
    # ĐỌC CHANNEL
    # ------------------------------------------------------

    channels = read_channels(args)

    if not channels:

        print(
            "Không có kênh nào trong channels.txt."
        )

        return


    # ------------------------------------------------------
    # SCAN CHANNELS
    # ------------------------------------------------------

    jobs: list[tuple[str, str]] = []


    for ch in channels:

        log(
            f"[KÊNH] {ch}"
        )

        try:

            entries = list_channel_videos(ch)

        except Exception as e:

            log(
                f"  Không đọc được kênh: {e}"
            )

            continue


        for e in entries:

            try:

                url, title, dur = video_meta(e)

            except Exception as ex:

                log(
                    f"  Bỏ qua "
                    f"(lỗi lấy thông tin): {ex}"
                )

                continue


            # ------------------------------------------------
            # FILTER DURATION
            # ------------------------------------------------

            if (
                dur is None
                or not (
                    MIN_DURATION
                    <= dur
                    <= MAX_DURATION
                )
            ):

                log(
                    f"  [BỎ QUA - độ dài "
                    f"{dur}s] {title}"
                )

                continue


            # ------------------------------------------------
            # CHECK DUPLICATE TITLE
            # ------------------------------------------------

            key = norm(title)

            with lock:

                if key in seen_titles:

                    log(
                        f"  [BỎ QUA - trùng tên] "
                        f"{title}"
                    )

                    continue


                # Giữ chỗ để tránh hai channel
                # tải cùng một video trong cùng lượt
                seen_titles.add(key)


            jobs.append(
                (url, title)
            )


    # ------------------------------------------------------
    # KHÔNG CÓ JOB
    # ------------------------------------------------------

    if not jobs:

        print()
        print(
            "Không có video mới để tải."
        )

        return


    # ------------------------------------------------------
    # BUILD YT-DLP OPTIONS
    # ------------------------------------------------------

    opts = build_opts(
        outdir,
        ffmpeg_path,
        ffprobe_path,
        aria2c_path,
    )


    # ------------------------------------------------------
    # DOWNLOAD
    # ------------------------------------------------------

    print()
    print(
        f"Sẽ tải {len(jobs)} video "
        f"với {args.workers} luồng..."
    )
    print()


    ok = 0


    with ThreadPoolExecutor(
        max_workers=max(
            1,
            args.workers
        )
    ) as pool:

        futures = [

            pool.submit(
                download_one,
                url,
                title,
                opts,
                titles_file,
            )

            for url, title in jobs
        ]


        for f in as_completed(futures):

            try:

                if f.result():

                    ok += 1

            except Exception as e:

                log(
                    f"[LỖI THREAD] {e}"
                )


    # ------------------------------------------------------
    # RESULT
    # ------------------------------------------------------

    print()
    print("=" * 70)
    print(
        f"Hoàn tất: {ok}/{len(jobs)} video."
    )
    print("=" * 70)


# ==========================================================
# ENTRY POINT
# ==========================================================

if __name__ == "__main__":
    main()