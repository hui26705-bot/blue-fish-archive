"""Generate WebP previews for sticker originals, in two tiers.

- Grid tier (previews/, 480px max, q80): the masonry wall shows cards at
  ~210px wide, so 480px comfortably covers 2x displays at a fraction of
  the bytes of the old 700px tier (~half the size).
- Large tier (large/, full size, q85): used by the lightbox, so viewing a
  sticker no longer downloads the multi-hundred-KB original PNG/JPG.
  Download/copy keep using the untouched original file.

Animated GIF/APNG originals stay animated: the grid tier becomes an animated
480px WebP, so the wall no longer downloads the multi-MB original just to show
a moving card. The original file is left untouched for download/copy.

MP4 originals take the same animated route: ffmpeg extracts a short preview
strip (12 frames) as animated WebP for the grid tier; the lightbox streams
the original MP4 directly, so no full-size `large/` copy is generated.
Runners provide ffmpeg (GitHub-hosted ubuntu); without it video files fail
loudly instead of silently vanishing from the wall.

Only previews that are missing or older than their source are regenerated,
so this is safe to run on every sync.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "media"
PREVIEW_DIR = ROOT / "previews"
LARGE_DIR = ROOT / "large"
SOURCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".apng"}
ANIMATED_EXTENSIONS = {".gif", ".apng"}
VIDEO_EXTENSIONS = {".mp4"}
# 视频预览条：12 帧约 2 秒循环，够认出是动图即可。
VIDEO_PREVIEW_FRAMES = 12
VIDEO_PREVIEW_FPS = 6
GRID_MAX_DIMENSION = 480
GRID_QUALITY = 80
# 动画档必须保住帧数和节奏: 动图一旦卡顿就失去意义,所以 grid 档靠降分辨率省字节,
# 而不是像静态图那样主要靠降质量。
ANIMATED_GRID_QUALITY = 75
LARGE_QUALITY = 85
# Pillow 的 WebP _save_all 默认 method=0,而单帧 _save 默认 method=4;不显式指定
# 会让动画档白白多出三成体积。这里与静态图路径对齐。
WEBP_METHOD = 4
DEFAULT_FRAME_DURATION_MS = 80
MIN_FRAME_DURATION_MS = 20


def _needs_update(target: Path, source: Path) -> bool:
    return not target.is_file() or target.stat().st_mtime < source.stat().st_mtime


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )


def _to_webp(path: Path, target: Path, quality: int, max_dimension: int | None) -> None:
    with Image.open(path) as image:
        if max_dimension is not None:
            image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        image = image.convert("RGBA" if _has_alpha(image) else "RGB")
        image.save(target, "WEBP", quality=quality)
    print(f"Generated {target.relative_to(ROOT).as_posix()}")


def _iter_animated_frames(path: Path) -> Iterator[tuple[Image.Image, int]]:
    """逐帧产出 RGBA 帧与其时长(GIF 的 duration 是"到下一帧的间隔")。"""
    with Image.open(path) as image:
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            duration = image.info.get("duration") or DEFAULT_FRAME_DURATION_MS
            yield image.convert("RGBA"), max(int(duration), MIN_FRAME_DURATION_MS)


def _to_animated_webp(
    path: Path, target: Path, quality: int, max_dimension: int | None
) -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame, duration in _iter_animated_frames(path):
        if max_dimension is not None:
            frame.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        frames.append(frame)
        durations.append(duration)
    if not frames:
        raise ValueError(f"No frames found in animated image: {path}")
    # 循环次数沿用源文件惯例(无限循环);large 档不设 max_dimension,保持原尺寸。
    frames[0].save(
        target,
        "WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        quality=quality,
        method=WEBP_METHOD,
    )
    print(f"Generated {target.relative_to(ROOT).as_posix()}")


def _to_video_preview(path: Path, target: Path, quality: int, max_dimension: int | None) -> None:
    """MP4 → 动画 WebP 预览条（只做 grid 档，灯箱直接播原片）。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(f"ffmpeg 缺失，无法为视频生成预览: {path.name}")
    scale = f"scale={max_dimension}:-2" if max_dimension is not None else "scale=-2:-2"
    cmd = [
        ffmpeg, "-y", "-v", "error", "-i", str(path),
        "-vf", f"fps={VIDEO_PREVIEW_FPS},{scale}",
        "-vframes", str(VIDEO_PREVIEW_FRAMES),
        "-c:v", "libwebp", "-quality", str(quality), "-loop", "0",
        str(target),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError(f"视频预览生成失败 {path.name}: {result.stderr.strip()[:300]}")
    print(f"Generated {target.relative_to(ROOT).as_posix()}")


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(f"Sticker source directory not found: {SOURCE_DIR}", file=sys.stderr)
        return 1

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    LARGE_DIR.mkdir(parents=True, exist_ok=True)

    generated = 0
    skipped = 0
    for path in sorted(SOURCE_DIR.iterdir()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS
        if not is_video and suffix not in SOURCE_EXTENSIONS:
            continue

        preview = PREVIEW_DIR / f"{path.stem}.webp"
        large = LARGE_DIR / f"{path.stem}.webp"
        if is_video:
            # 视频只做 grid 动画预览条，灯箱直接播原 mp4，不占 large 档。
            if _needs_update(preview, path):
                _to_video_preview(path, preview, GRID_QUALITY, GRID_MAX_DIMENSION)
                generated += 1
            else:
                skipped += 1
            continue
        animated = suffix in ANIMATED_EXTENSIONS
        encode = _to_animated_webp if animated else _to_webp
        grid_quality = ANIMATED_GRID_QUALITY if animated else GRID_QUALITY

        if not _needs_update(preview, path) and not _needs_update(large, path):
            skipped += 1
            continue

        if _needs_update(preview, path):
            encode(path, preview, grid_quality, GRID_MAX_DIMENSION)
            generated += 1
        if _needs_update(large, path):
            encode(path, large, LARGE_QUALITY, None)
            generated += 1

    print(f"Done. {generated} generated, {skipped} up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
