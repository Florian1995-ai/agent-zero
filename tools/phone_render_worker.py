#!/usr/bin/env python3
"""
Server-side short render worker for phone uploads.

This script runs inside the Agent Zero container. It intentionally accepts no
arbitrary shell commands from HTTP: it picks the newest stable, ffprobe-valid
upload from a fixed directory and writes outputs to a fixed assets directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UPLOAD_DIR = Path(os.getenv("AGENTZERO_UPLOAD_DIR", "/app/work_dir/assets/agentzero_uploads/shorts_test"))
OUTPUT_ROOT = Path(os.getenv("AGENTZERO_OUTPUT_DIR", "/app/work_dir/assets/agentzero_outputs"))
STATE_FILE = OUTPUT_ROOT / "render_status.json"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
PARTIAL_SUFFIXES = {".part", ".tmp", ".download", ".crdownload"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, max_len: int = 54) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return (value or "video")[:max_len].strip("-") or "video"


def write_status(**updates: Any) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if STATE_FILE.exists():
        try:
            current = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(updates)
    current["updated_at"] = utc_now()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def run(cmd: list[str], step: str, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"[{step}] {' '.join(cmd)}", flush=True)
    write_status(step=step)
    if capture:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    else:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True)
    if result.returncode != 0:
        stderr = result.stderr[-4000:] if result.stderr else ""
        raise RuntimeError(f"{step} failed with exit code {result.returncode}\n{stderr}")
    return result


def ffprobe_json(path: Path) -> dict[str, Any]:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        "ffprobe",
        capture=True,
    )
    return json.loads(result.stdout)


def ffprobe_duration(path: Path) -> float:
    data = ffprobe_json(path)
    duration = float(data.get("format", {}).get("duration") or 0)
    return duration


def is_stable(path: Path, checks: int = 2, delay: float = 1.5) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if path.suffix.lower() in PARTIAL_SUFFIXES:
        return False
    previous = path.stat().st_size
    if previous <= 1024 * 1024:
        return False
    for _ in range(checks):
        time.sleep(delay)
        current = path.stat().st_size
        if current != previous or current <= 1024 * 1024:
            return False
        previous = current
    return True


def has_video_stream(probe: dict[str, Any]) -> bool:
    return any(stream.get("codec_type") == "video" for stream in probe.get("streams", []))


def newest_valid_upload() -> tuple[Path, dict[str, Any]]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [
        path
        for path in UPLOAD_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and path.suffix.lower() not in PARTIAL_SUFFIXES
    ]
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    checked: list[dict[str, Any]] = []
    for path in candidates:
        entry = {"file": path.name, "size_mb": round(path.stat().st_size / (1024 * 1024), 1)}
        try:
            if not is_stable(path):
                entry["ok"] = False
                entry["reason"] = "not stable or too small"
                checked.append(entry)
                continue
            probe = ffprobe_json(path)
            duration = float(probe.get("format", {}).get("duration") or 0)
            entry["duration"] = round(duration, 2)
            if duration < 1 or not has_video_stream(probe):
                entry["ok"] = False
                entry["reason"] = "ffprobe invalid"
                checked.append(entry)
                continue
            entry["ok"] = True
            checked.append(entry)
            write_status(checked_uploads=checked)
            return path, probe
        except Exception as exc:
            entry["ok"] = False
            entry["reason"] = str(exc)[:300]
            checked.append(entry)

    write_status(checked_uploads=checked)
    raise RuntimeError(f"No stable ffprobe-valid uploads found in {UPLOAD_DIR}")


def make_job_dir(source: Path) -> Path:
    job_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{slugify(source.stem)}"
    job_dir = OUTPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    return job_dir


def normalize_video(input_path: Path, output_path: Path) -> None:
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1,fps=30,format=yuv420p"
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        "normalize",
    )


def parse_silences(stderr: str) -> list[tuple[float, float]]:
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", stderr)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", stderr)]
    return [(start, end) for start, end in zip(starts, ends) if end > start]


def speech_segments(duration: float, silences: list[tuple[float, float]], padding: float = 0.05) -> list[tuple[float, float]]:
    if not silences:
        return [(0.0, duration)]

    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for silence_start, silence_end in silences:
        start = cursor
        end = max(cursor, silence_start + padding)
        if end - start >= 0.18:
            keep.append((start, min(duration, end)))
        cursor = max(cursor, silence_end - padding)

    if duration - cursor >= 0.18:
        keep.append((cursor, duration))

    if not keep:
        return [(0.0, duration)]
    return keep


def cut_silences(input_path: Path, output_path: Path, job_dir: Path) -> int:
    detect = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(input_path),
            "-af",
            "silencedetect=noise=-35dB:d=0.28",
            "-f",
            "null",
            "-",
        ],
        "detect-silence",
        capture=True,
    )
    silences = parse_silences((detect.stderr or "") + "\n" + (detect.stdout or ""))
    duration = ffprobe_duration(input_path)
    segments = speech_segments(duration, silences)
    (job_dir / "segments.json").write_text(
        json.dumps(
            {
                "duration": duration,
                "silences": silences,
                "segments": segments,
                "cut_count": max(0, len(segments) - 1),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if len(segments) <= 1:
        shutil.copy2(input_path, output_path)
        return 0

    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, (start, end) in enumerate(segments):
        filters.append(f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]")
        filters.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]")
        concat_inputs.append(f"[v{index}][a{index}]")
    filter_complex = ";".join(filters) + ";" + "".join(concat_inputs) + f"concat=n={len(segments)}:v=1:a=1[outv][outa]"

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        "jump-cuts",
    )
    return max(0, len(segments) - 1)


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    return f"{hours}:{minutes:02d}:{whole:02d}.{centis:02d}"


def ass_escape(text: str) -> str:
    return text.replace("{", "").replace("}", "").replace("\n", " ").strip()


def build_caption_groups(words: list[dict[str, Any]]) -> list[tuple[float, float, str]]:
    groups: list[tuple[float, float, str]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        start = float(current[0].get("start", 0))
        end = float(current[-1].get("end", start + 0.8))
        text = " ".join(str(word.get("word", "")).strip() for word in current)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            groups.append((start, max(end, start + 0.45), text.upper()))
        current = []

    for word in words:
        value = str(word.get("word", "")).strip()
        if not value:
            continue
        current.append(word)
        text = " ".join(str(item.get("word", "")).strip() for item in current)
        duration = float(current[-1].get("end", 0)) - float(current[0].get("start", 0))
        if len(current) >= 3 or len(text) >= 18 or duration >= 1.0:
            flush()
    flush()
    return groups


def transcribe_and_write_captions(video_path: Path, job_dir: Path) -> Path | None:
    write_status(step="transcribe")
    try:
        import whisper  # type: ignore

        model_name = os.getenv("WHISPER_MODEL", "base")
        print(f"[transcribe] loading whisper model: {model_name}", flush=True)
        model = whisper.load_model(model_name)
        result = model.transcribe(str(video_path), fp16=False, word_timestamps=True)
        transcript_path = job_dir / "transcript.json"
        transcript_path.write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")

        words: list[dict[str, Any]] = []
        for segment in result.get("segments", []):
            segment_words = segment.get("words") or []
            if segment_words:
                words.extend(segment_words)
            else:
                text_words = str(segment.get("text", "")).split()
                start = float(segment.get("start", 0))
                end = float(segment.get("end", start + 1))
                span = max(0.4, (end - start) / max(1, len(text_words)))
                for index, value in enumerate(text_words):
                    words.append({"word": value, "start": start + index * span, "end": start + (index + 1) * span})

        groups = build_caption_groups(words)
        ass_path = job_dir / "captions.ass"
        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "WrapStyle: 0",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Hormozi,Arial,86,&H00FFFFFF,&H0000FFFF,&H00000000,&H88000000,-1,0,0,0,100,100,0,0,1,7,2,2,70,70,285,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for start, end, text in groups:
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Hormozi,,0,0,0,,{ass_escape(text)}")
        ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        write_status(transcript=str(transcript_path), captions=str(ass_path), caption_count=len(groups))
        return ass_path if groups else None
    except Exception as exc:
        write_status(captions_error=str(exc)[:500])
        print(f"[transcribe] failed, rendering without captions: {exc}", flush=True)
        return None


def burn_captions_or_copy(input_path: Path, output_path: Path, captions_path: Path | None) -> None:
    if captions_path and captions_path.exists():
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-vf",
                f"subtitles={captions_path}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            "burn-captions",
        )
    else:
        shutil.copy2(input_path, output_path)


def make_thumbnail(video_path: Path, output_path: Path) -> None:
    duration = ffprobe_duration(video_path)
    seek = max(0.0, min(1.5, duration / 3))
    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{seek:.2f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ],
        "thumbnail",
    )


def main() -> int:
    write_status(state="running", started_at=utc_now(), upload_dir=str(UPLOAD_DIR), output_root=str(OUTPUT_ROOT))
    try:
        source, probe = newest_valid_upload()
        job_dir = make_job_dir(source)
        log_path = job_dir / "render.log"
        write_status(state="running", input=str(source), job_dir=str(job_dir), log=str(log_path))

        raw_input = job_dir / f"raw_input{source.suffix.lower()}"
        normalized = job_dir / "normalized.mp4"
        edited = job_dir / "edited_base.mp4"
        final = job_dir / "final.mp4"
        thumbnail = job_dir / "thumbnail.jpg"

        shutil.copy2(source, raw_input)
        (job_dir / "input_probe.json").write_text(json.dumps(probe, indent=2, ensure_ascii=True), encoding="utf-8")

        normalize_video(raw_input, normalized)
        cut_count = cut_silences(normalized, edited, job_dir)
        captions = transcribe_and_write_captions(edited, job_dir)
        burn_captions_or_copy(edited, final, captions)
        make_thumbnail(final, thumbnail)

        final_duration = ffprobe_duration(final)
        metadata = {
            "privacy_status": "private",
            "style": "tight jump cuts, bold burned-in short-form captions",
            "source": str(source),
            "job_dir": str(job_dir),
            "final": str(final),
            "thumbnail": str(thumbnail),
            "duration": final_duration,
            "cut_count": cut_count,
            "completed_at": utc_now(),
        }
        (job_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
        write_status(
            state="complete",
            step="complete",
            final=str(final),
            thumbnail=str(thumbnail),
            metadata=str(job_dir / "metadata.json"),
            final_size_mb=round(final.stat().st_size / (1024 * 1024), 1),
            thumbnail_size_kb=round(thumbnail.stat().st_size / 1024, 1),
            duration=round(final_duration, 2),
            cut_count=cut_count,
            completed_at=utc_now(),
        )
        print(f"[complete] {final}", flush=True)
        return 0
    except Exception as exc:
        write_status(state="failed", step="failed", error=str(exc), traceback=traceback.format_exc()[-6000:])
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
