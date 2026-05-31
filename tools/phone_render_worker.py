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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UPLOAD_DIR = Path(os.getenv("AGENTZERO_UPLOAD_DIR", "/app/work_dir/assets/agentzero_uploads/shorts_test"))
OUTPUT_ROOT = Path(os.getenv("AGENTZERO_OUTPUT_DIR", "/app/work_dir/assets/agentzero_outputs"))
PIPELINE_DIR = Path(os.getenv("AGENTZERO_PIPELINE_DIR", "/app/work_dir/assets/agentzero_pipeline"))
PIPELINE_CONFIG_PATH = Path(os.getenv("AGENTZERO_PIPELINE_CONFIG", str(PIPELINE_DIR / "pipeline_config.json")))
STATE_FILE = OUTPUT_ROOT / "render_status.json"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
PARTIAL_SUFFIXES = {".part", ".tmp", ".download", ".crdownload"}
STAGE_TIMINGS: list[dict[str, Any]] = []


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_pipeline_config() -> dict[str, Any]:
    if not PIPELINE_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(PIPELINE_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[config] failed to read {PIPELINE_CONFIG_PATH}: {exc}", flush=True)
        return {}


PIPELINE_CONFIG = load_pipeline_config()


def cfg(section: str, key: str, default: Any) -> Any:
    value = PIPELINE_CONFIG.get(section, {})
    if isinstance(value, dict):
        return value.get(key, default)
    return default


def cfg_float(section: str, key: str, default: float) -> float:
    try:
        return float(cfg(section, key, default))
    except (TypeError, ValueError):
        return default


def cfg_int(section: str, key: str, default: int) -> int:
    try:
        return int(cfg(section, key, default))
    except (TypeError, ValueError):
        return default


def cfg_bool(section: str, key: str, default: bool) -> bool:
    value = cfg(section, key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


EDITING_PRESET = {
    "name": "agentzero_hostinger_v2_locked_silence_2026_05_31",
    "description": "Locked preset from the first successful Hostinger phone render.",
    "normalize": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "video_codec": "libx264",
        "preset": "veryfast",
        "crf": 20,
        "audio_codec": "aac",
        "audio_bitrate": "192k",
        "audio_loudnorm": "I=-16:TP=-1.5:LRA=11",
    },
    "silence_cut": {
        "detector": "ffmpeg silencedetect",
        "noise": "-28dB",
        "duration": 0.18,
        "padding_seconds": 0.035,
        "min_kept_segment_seconds": 0.12,
    },
    "word_gap_fallback": {
        "enabled": True,
        "max_gap_seconds": 0.22,
        "padding_seconds": 0.035,
        "caption_timestamp_remap": True,
    },
    "captions": {
        "style": "bold white uppercase with black outline",
        "font": "Arial",
        "font_size": 86,
        "alignment": "lower third center",
    },
}
if isinstance(PIPELINE_CONFIG.get("editing_preset"), dict):
    EDITING_PRESET = deep_merge(EDITING_PRESET, PIPELINE_CONFIG["editing_preset"])

AUDIO_ASSET_DIR = Path(os.getenv("AGENTZERO_AUDIO_ASSET_DIR", str(cfg("audio", "asset_dir", PIPELINE_DIR / "audio"))))
YOUTUBE_CREDENTIALS_PATH = Path(os.getenv("YOUTUBE_CREDENTIALS_PATH", str(cfg("youtube", "credentials_path", "/app/work_dir/assets/youtube/credentials.json"))))
YOUTUBE_TOKEN_PATH = Path(os.getenv("YOUTUBE_TOKEN_PATH", str(cfg("youtube", "token_path", "/app/work_dir/assets/youtube/token.json"))))


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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")


def run(cmd: list[str], step: str, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"[{step}] {' '.join(cmd)}", flush=True)
    write_status(step=step)
    started = time.perf_counter()
    if capture:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    else:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True)
    elapsed = round(time.perf_counter() - started, 3)
    STAGE_TIMINGS.append({"step": step, "seconds": elapsed, "returncode": result.returncode})
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
    normalize = EDITING_PRESET.get("normalize", {})
    width = int(normalize.get("width", 1080))
    height = int(normalize.get("height", 1920))
    fps = int(normalize.get("fps", 30))
    preset = str(normalize.get("preset", "veryfast"))
    crf = str(normalize.get("crf", 20))
    audio_bitrate = str(normalize.get("audio_bitrate", "192k"))
    loudnorm = str(normalize.get("audio_loudnorm", "I=-16:TP=-1.5:LRA=11"))
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={fps},format=yuv420p"
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
            f"loudnorm={loudnorm}",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
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


def speech_segments(
    duration: float,
    silences: list[tuple[float, float]],
    padding: float = 0.035,
    min_kept_segment: float = 0.12,
) -> list[tuple[float, float]]:
    if not silences:
        return [(0.0, duration)]

    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for silence_start, silence_end in silences:
        start = cursor
        end = max(cursor, silence_start + padding)
        if end - start >= min_kept_segment:
            keep.append((start, min(duration, end)))
        cursor = max(cursor, silence_end - padding)

    if duration - cursor >= min_kept_segment:
        keep.append((cursor, duration))

    if not keep:
        return [(0.0, duration)]
    return keep


def render_segments(input_path: Path, output_path: Path, segments: list[tuple[float, float]], step: str) -> int:
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
        step,
    )
    return max(0, len(segments) - 1)


def cut_silences(input_path: Path, output_path: Path, job_dir: Path) -> int:
    silence = EDITING_PRESET.get("silence_cut", {})
    noise = str(silence.get("noise", "-28dB"))
    silence_duration = float(silence.get("duration", 0.18))
    padding = float(silence.get("padding_seconds", 0.035))
    min_kept = float(silence.get("min_kept_segment_seconds", 0.12))
    detect = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(input_path),
            "-af",
            f"silencedetect=noise={noise}:d={silence_duration}",
            "-f",
            "null",
            "-",
        ],
        "detect-silence",
        capture=True,
    )
    silences = parse_silences((detect.stderr or "") + "\n" + (detect.stdout or ""))
    duration = ffprobe_duration(input_path)
    segments = speech_segments(duration, silences, padding=padding, min_kept_segment=min_kept)
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
    return render_segments(input_path, output_path, segments, "jump-cuts")


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    return f"{hours}:{minutes:02d}:{whole:02d}.{centis:02d}"


def ass_escape(text: str) -> str:
    return text.replace("{", "").replace("}", "").replace("\n", " ").strip()


def plain_text_from_words(words: list[dict[str, Any]]) -> str:
    return re.sub(r"\s+", " ", " ".join(str(word.get("word", "")).strip() for word in words)).strip()


def shift_words(words: list[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    shifted = []
    for word in words:
        if word.get("start") is None or word.get("end") is None:
            continue
        copy = dict(word)
        copy["start"] = float(copy["start"]) + offset
        copy["end"] = float(copy["end"]) + offset
        shifted.append(copy)
    return shifted


def words_for_segments(words: list[dict[str, Any]], segments: list[tuple[float, float]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    offset = 0.0
    for seg_start, seg_end in segments:
        seg_duration = max(0.0, seg_end - seg_start)
        for word in words:
            if word.get("start") is None or word.get("end") is None:
                continue
            word_start = float(word["start"])
            word_end = float(word["end"])
            if word_end < seg_start or word_start > seg_end:
                continue
            copy = dict(word)
            copy["start"] = offset + max(word_start, seg_start) - seg_start
            copy["end"] = offset + min(word_end, seg_end) - seg_start
            if copy["end"] > copy["start"]:
                selected.append(copy)
        offset += seg_duration
    return selected


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


def write_ass_captions(words: list[dict[str, Any]], job_dir: Path, filename: str = "captions.ass") -> Path | None:
    groups = build_caption_groups(words)
    ass_path = job_dir / filename
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
    write_status(captions=str(ass_path), caption_count=len(groups))
    return ass_path if groups else None


def transcribe_and_write_captions(video_path: Path, job_dir: Path) -> tuple[Path | None, list[dict[str, Any]]]:
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

        (job_dir / "words.json").write_text(json.dumps(words, indent=2, ensure_ascii=True), encoding="utf-8")
        ass_path = write_ass_captions(words, job_dir)
        write_status(transcript=str(transcript_path))
        return ass_path, words
    except Exception as exc:
        write_status(captions_error=str(exc)[:500])
        print(f"[transcribe] failed, rendering without captions: {exc}", flush=True)
        return None, []


def word_gap_segments(words: list[dict[str, Any]], duration: float, max_gap: float = 0.22, padding: float = 0.035) -> list[tuple[float, float]]:
    clean_words = [
        word for word in words
        if word.get("start") is not None and word.get("end") is not None and str(word.get("word", "")).strip()
    ]
    if len(clean_words) < 2:
        return [(0.0, duration)]

    segments: list[tuple[float, float]] = []
    start = max(0.0, float(clean_words[0]["start"]) - padding)
    last_end = float(clean_words[0]["end"])

    for word in clean_words[1:]:
        word_start = float(word["start"])
        word_end = float(word["end"])
        gap = word_start - last_end
        if gap >= max_gap:
            end = min(duration, last_end + padding)
            if end - start >= 0.12:
                segments.append((start, end))
            start = max(0.0, word_start - padding)
        last_end = max(last_end, word_end)

    end = min(duration, last_end + padding)
    if end - start >= 0.12:
        segments.append((start, end))
    return segments or [(0.0, duration)]


def remap_words_to_segments(words: list[dict[str, Any]], segments: list[tuple[float, float]]) -> list[dict[str, Any]]:
    remapped: list[dict[str, Any]] = []
    offset = 0.0
    for seg_start, seg_end in segments:
        seg_duration = max(0.0, seg_end - seg_start)
        for word in words:
            if word.get("start") is None or word.get("end") is None:
                continue
            word_start = float(word["start"])
            word_end = float(word["end"])
            if word_end < seg_start or word_start > seg_end:
                continue
            copy = dict(word)
            copy["start"] = offset + max(word_start, seg_start) - seg_start
            copy["end"] = offset + min(word_end, seg_end) - seg_start
            if copy["end"] > copy["start"]:
                remapped.append(copy)
        offset += seg_duration
    return remapped


def keyword_tags(text: str, limit: int = 8) -> list[str]:
    stopwords = {
        "about", "after", "again", "also", "and", "are", "because", "but", "can",
        "for", "from", "have", "into", "just", "like", "that", "the", "this",
        "was", "what", "when", "where", "with", "you", "your",
    }
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text.lower())
    counts: dict[str, int] = {}
    for word in words:
        if word in stopwords:
            continue
        counts[word] = counts.get(word, 0) + 1
    return [word for word, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def candidate_nuggets(words: list[dict[str, Any]], duration: float, limit: int = 8) -> list[dict[str, Any]]:
    clean = [
        word for word in words
        if word.get("start") is not None and word.get("end") is not None and str(word.get("word", "")).strip()
    ]
    if not clean:
        return []

    hook_terms = {
        "ai", "agent", "automate", "automation", "business", "client", "content",
        "cost", "growth", "lead", "money", "offer", "pipeline", "result", "sales",
        "system", "time", "video", "youtube",
    }
    candidates: list[dict[str, Any]] = []
    window = 11
    step = 6
    for start_index in range(0, max(1, len(clean) - window + 1), step):
        chunk = clean[start_index:start_index + window]
        if len(chunk) < 4:
            continue
        start = max(0.0, float(chunk[0]["start"]) - 0.035)
        end = min(duration, float(chunk[-1]["end"]) + 0.035)
        clip_duration = end - start
        if clip_duration < 1.2 or clip_duration > 4.0:
            continue
        text = plain_text_from_words(chunk)
        lower = text.lower()
        score = 1.0
        score += sum(1.5 for term in hook_terms if term in lower)
        score += 0.4 if "?" in text else 0
        score += min(2.0, len(text) / 55)
        candidates.append({"start": start, "end": end, "text": text, "score": round(score, 2)})

    candidates.sort(key=lambda item: item["score"], reverse=True)
    picked: list[dict[str, Any]] = []
    for candidate in candidates:
        overlaps = any(
            not (candidate["end"] <= got["start"] or candidate["start"] >= got["end"])
            for got in picked
        )
        if not overlaps:
            picked.append(candidate)
        if len(picked) >= limit:
            break

    if not picked and clean:
        end_index = min(len(clean), 10)
        picked.append({
            "start": max(0.0, float(clean[0]["start"]) - 0.035),
            "end": min(duration, float(clean[end_index - 1]["end"]) + 0.035),
            "text": plain_text_from_words(clean[:end_index]),
            "score": 1.0,
        })
    return picked


def fallback_analysis(words: list[dict[str, Any]], duration: float, reason: str) -> dict[str, Any]:
    text = plain_text_from_words(words)
    first_sentence = re.split(r"(?<=[.!?])\s+", text)[0][:80].strip()
    title = first_sentence or "AI Short"
    tags = keyword_tags(text)
    return {
        "source": "local_fallback",
        "reason": reason,
        "title": title,
        "description": (
            f"{title}\n\n"
            "Short-form clip rendered automatically by the AgentZero Hostinger pipeline."
        ),
        "tags": tags,
        "nuggets": candidate_nuggets(words, duration, limit=3),
        "hook": title,
    }


def estimate_openrouter_cost(prompt_chars: int, output_tokens: int = 900, model: str = "") -> dict[str, Any]:
    prompt_tokens = max(1, int(prompt_chars / 4))
    default_prompt_price = 1.25
    default_completion_price = 10.0
    if "gpt-4.1-mini" in model.lower():
        default_prompt_price = 0.4
        default_completion_price = 1.6
    if os.getenv("OPENROUTER_PROMPT_PRICE_PER_MILLION"):
        prompt_price_per_million = env_float("OPENROUTER_PROMPT_PRICE_PER_MILLION", default_prompt_price)
    elif "gpt-4.1-mini" in model.lower():
        prompt_price_per_million = default_prompt_price
    else:
        prompt_price_per_million = cfg_float("llm", "prompt_price_per_million", default_prompt_price)

    if os.getenv("OPENROUTER_COMPLETION_PRICE_PER_MILLION"):
        completion_price_per_million = env_float("OPENROUTER_COMPLETION_PRICE_PER_MILLION", default_completion_price)
    elif "gpt-4.1-mini" in model.lower():
        completion_price_per_million = default_completion_price
    else:
        completion_price_per_million = cfg_float("llm", "completion_price_per_million", default_completion_price)
    estimated = (prompt_tokens / 1_000_000 * prompt_price_per_million) + (
        output_tokens / 1_000_000 * completion_price_per_million
    )
    return {
        "prompt_tokens_estimate": prompt_tokens,
        "completion_tokens_budget": output_tokens,
        "estimated_cost_usd": round(estimated, 6),
        "prompt_price_per_million": prompt_price_per_million,
        "completion_price_per_million": completion_price_per_million,
    }


LLM_ANALYSIS_SCHEMA = {
    "name": "shortform_transcript_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Compelling YouTube Shorts title, under 70 characters when possible.",
            },
            "description": {
                "type": "string",
                "description": "Short YouTube description with one concise paragraph and optional hashtags.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Searchable YouTube tags.",
            },
            "hook": {
                "type": "string",
                "description": "One-line reason this short is interesting.",
            },
            "nuggets": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "text": {"type": "string"},
                        "reason": {"type": "string"},
                        "score": {"type": "number"},
                    },
                    "required": ["start", "end", "text", "reason", "score"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "description", "tags", "hook", "nuggets"],
        "additionalProperties": False,
    },
}


def parse_llm_json(content: Any) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response content was empty")
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    match = re.search(r"\{.*\}", content, flags=re.S)
    if match:
        content = match.group(0)
    return json.loads(content)


def list_from_config_or_env(env_name: str, section: str, key: str, default: list[str]) -> list[str]:
    raw_env = os.getenv(env_name, "").strip()
    raw_value: Any = raw_env or cfg(section, key, default)
    values: list[str] = []
    if isinstance(raw_value, list):
        values = [str(item).strip() for item in raw_value]
    elif isinstance(raw_value, str):
        values = [item.strip() for item in raw_value.split(",")]
    values = [value for value in values if value]
    return values or default


def openrouter_models() -> list[str]:
    env_model = os.getenv("OPENROUTER_MODEL", "").strip()
    config_model = str(cfg("llm", "model", "openai/gpt-4.1-mini")).strip()
    config_fallbacks = cfg("llm", "fallback_models", [])
    primary = env_model or config_model or "openai/gpt-4.1-mini"
    if not env_model and primary == "openai/gpt-5" and not config_fallbacks:
        primary = "openai/gpt-4.1-mini"
    fallbacks = list_from_config_or_env(
        "OPENROUTER_FALLBACK_MODELS",
        "llm",
        "fallback_models",
        ["google/gemini-2.5-flash", "openai/gpt-5"],
    )
    ordered = [primary or "openai/gpt-4.1-mini", *fallbacks]
    deduped: list[str] = []
    for model in ordered:
        if model and model not in deduped:
            deduped.append(model)
    return deduped


def extract_message_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenRouter response had no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError("OpenRouter response had no message")

    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        content = "\n".join(part for part in parts if part.strip())

    candidates = [
        content,
        message.get("reasoning_content"),
        message.get("reasoning"),
        data.get("output_text"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    raise ValueError("OpenRouter response content was empty")


def normalize_analysis(parsed: dict[str, Any], words: list[dict[str, Any]], duration: float, transcript_text: str) -> dict[str, Any]:
    fallback = fallback_analysis(words, duration, "missing field")
    parsed["source"] = "openrouter"
    parsed["title"] = str(parsed.get("title") or fallback["title"]).strip()[:100]
    parsed["description"] = str(parsed.get("description") or fallback["description"]).strip()[:4500]
    tags = parsed.get("tags")
    if not isinstance(tags, list):
        tags = keyword_tags(transcript_text)
    parsed["tags"] = [str(tag).strip()[:40] for tag in tags if str(tag).strip()][:12]
    parsed["hook"] = str(parsed.get("hook") or parsed["title"]).strip()[:180]
    parsed["nuggets"] = normalize_nuggets(parsed.get("nuggets"), words, duration)
    if len(parsed["nuggets"]) < 2:
        raise ValueError("OpenRouter returned too few usable nuggets")
    return parsed


def normalize_nuggets(raw_nuggets: Any, words: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    if not isinstance(raw_nuggets, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in raw_nuggets:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, min(duration, float(item.get("start", 0))))
            end = max(start, min(duration, float(item.get("end", start + 2.0))))
        except (TypeError, ValueError):
            continue
        if end - start < 0.8:
            continue
        normalized.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": str(item.get("text", "")).strip(),
            "reason": str(item.get("reason", "")).strip(),
            "score": item.get("score", None),
        })

    picked: list[dict[str, Any]] = []
    total = 0.0
    max_seconds = cfg_float("intro_teaser", "max_seconds", 8.5)
    target_seconds = cfg_float("intro_teaser", "target_seconds", 8.0)
    max_nuggets = cfg_int("intro_teaser", "max_nuggets", 3)
    for item in normalized[:5]:
        length = item["end"] - item["start"]
        if total >= max(0.8, target_seconds - 0.5):
            break
        if total + length > max_seconds:
            item = dict(item)
            item["end"] = round(item["start"] + max(1.0, target_seconds - total), 3)
            length = item["end"] - item["start"]
        picked.append(item)
        total += length

    if not picked:
        return candidate_nuggets(words, duration, limit=max_nuggets)
    return picked[:max_nuggets]


def analyze_transcript(words: list[dict[str, Any]], duration: float, job_dir: Path) -> dict[str, Any]:
    provider = os.getenv("LLM_PROVIDER", str(cfg("llm", "provider", "openrouter"))).strip().lower()
    transcript_text = plain_text_from_words(words)
    usage_path = job_dir / "llm_usage.json"

    if provider in {"", "none", "off", "false"}:
        analysis = fallback_analysis(words, duration, "LLM_PROVIDER disabled")
        save_json(usage_path, {"provider": provider or "none", "used_llm": False, "reason": analysis["reason"]})
        return analysis

    if provider != "openrouter":
        analysis = fallback_analysis(words, duration, f"Unsupported LLM_PROVIDER={provider}")
        save_json(usage_path, {"provider": provider, "used_llm": False, "reason": analysis["reason"]})
        return analysis

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        analysis = fallback_analysis(words, duration, "OPENROUTER_API_KEY missing")
        save_json(usage_path, {"provider": "openrouter", "used_llm": False, "reason": analysis["reason"]})
        return analysis

    compact_words = [
        {
            "start": round(float(word.get("start", 0)), 2),
            "end": round(float(word.get("end", 0)), 2),
            "word": str(word.get("word", "")).strip(),
        }
        for word in words[:1800]
        if str(word.get("word", "")).strip()
    ]
    deterministic_candidates = candidate_nuggets(words, duration, limit=12)
    prompt = (
        "You are a senior short-form editor cutting talking-head business content for YouTube Shorts.\n"
        "Return only valid JSON matching the schema. No markdown, no explanations outside JSON.\n\n"
        "Task:\n"
        "- Pick 2-3 high-value teaser nuggets for a fast GaryVee-style cold open.\n"
        "- Total teaser target: 5-8 seconds.\n"
        "- The teaser is duplicated before the full edited content, so do not summarize; choose actual spoken moments.\n"
        "- Prefer concrete, curiosity-building claims, pain points, contrarian lines, numbers, or strong promises.\n"
        "- Avoid filler, greetings, setup-only phrases, and clips that feel mid-sentence.\n"
        "- Use word-safe start/end timestamps from the word list. Start at the first useful word and end after a complete clause.\n"
        "- Return nuggets sorted by editorial strength, highest score first.\n"
        "- Write a compelling title under 70 characters when possible, a concise description, tags, and a one-line hook.\n\n"
        "JSON shape:\n"
        "{\"title\": string, \"description\": string, \"tags\": string[], "
        "\"hook\": string, \"nuggets\": [{\"start\": number, \"end\": number, \"text\": string, "
        "\"reason\": string, \"score\": number}]}\n\n"
        f"Deterministic candidate nugget windows, use or refine these when they are strong:\n{json.dumps(deterministic_candidates, ensure_ascii=True)}\n\n"
        f"Transcript text:\n{transcript_text[:12000]}\n\n"
        f"Word timestamps JSON:\n{json.dumps(compact_words, ensure_ascii=True)}"
    )
    models = openrouter_models()
    estimate = estimate_openrouter_cost(len(prompt), model=models[0])
    max_cost = env_float("LLM_MAX_COST_PER_JOB_USD", cfg_float("llm", "max_cost_per_job_usd", 0.20))
    if estimate["estimated_cost_usd"] > max_cost:
        analysis = fallback_analysis(words, duration, "Estimated LLM cost above cap")
        save_json(usage_path, {
            "provider": "openrouter",
            "used_llm": False,
            "reason": analysis["reason"],
            "estimate": estimate,
            "max_cost_usd": max_cost,
        })
        return analysis

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://agent-zero-upload.2.24.108.202.sslip.io",
        "X-Title": "AgentZero ShortForm Renderer",
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You output parseable JSON only. Select short-form teaser clips with exact word-safe timestamps. "
                "Never return an empty message."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    def make_payload(model_name: str, mode: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 900,
            "stream": False,
        }
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": LLM_ANALYSIS_SCHEMA,
            }
            payload["provider"] = {"require_parameters": True}
            payload["plugins"] = [{"id": "response-healing"}]
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
            payload["plugins"] = [{"id": "response-healing"}]
        return payload

    def post_openrouter(payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(exc)
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body[:700]}") from exc

    attempts: list[dict[str, Any]] = []
    modes = ["json_schema", "json_object", "plain_json"]
    write_status(step="llm-analysis", llm_model=models[0], llm_status="running", llm_attempts=0)

    for model in models:
        for mode in modes:
            attempt = {"model": model, "mode": mode, "ok": False}
            attempts.append(attempt)
            write_status(step="llm-analysis", llm_model=model, llm_mode=mode, llm_attempts=len(attempts))
            try:
                data = post_openrouter(make_payload(model, mode))
                content = extract_message_content(data)
                parsed = parse_llm_json(content)
                analysis = normalize_analysis(parsed, words, duration, transcript_text)
                usage = data.get("usage", {}) if isinstance(data, dict) else {}
                actual_cost = usage.get("cost") or usage.get("total_cost") or data.get("cost")
                attempt["ok"] = True
                attempt["usage"] = usage
                save_json(usage_path, {
                    "provider": "openrouter",
                    "model": model,
                    "mode": mode,
                    "models_attempted": models,
                    "attempts": attempts,
                    "used_llm": True,
                    "estimate": estimate,
                    "max_cost_usd": max_cost,
                    "usage": usage,
                    "actual_cost_usd": actual_cost,
                })
                write_status(
                    llm_status="complete",
                    llm_model=model,
                    llm_mode=mode,
                    llm_attempts=len(attempts),
                    llm_estimated_cost_usd=estimate["estimated_cost_usd"],
                    llm_actual_cost_usd=actual_cost,
                    llm_error="",
                )
                return analysis
            except (RuntimeError, urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError) as exc:
                attempt["error"] = str(exc)[:700]
                continue

    reason = attempts[-1].get("error", "OpenRouter produced no usable analysis") if attempts else "OpenRouter produced no usable analysis"
    analysis = fallback_analysis(words, duration, f"OpenRouter failed after {len(attempts)} attempts: {reason}")
    save_json(usage_path, {
        "provider": "openrouter",
        "models_attempted": models,
        "attempts": attempts,
        "used_llm": False,
        "reason": analysis["reason"],
        "estimate": estimate,
        "max_cost_usd": max_cost,
    })
    write_status(llm_status="fallback", llm_attempts=len(attempts), llm_error=analysis["reason"][:400])
    return analysis


def build_intro_teaser(content_video: Path, words: list[dict[str, Any]], analysis: dict[str, Any], job_dir: Path) -> tuple[Path, list[dict[str, Any]], float, list[dict[str, Any]]]:
    if not cfg_bool("intro_teaser", "enabled", True):
        save_json(job_dir / "intro_nuggets.json", {"enabled": False, "nuggets": [], "segments": [], "duration": 0.0})
        write_status(intro_teaser="", intro_duration=0.0, intro_nuggets=0)
        return content_video, [], 0.0, []

    duration = ffprobe_duration(content_video)
    max_nuggets = cfg_int("intro_teaser", "max_nuggets", 3)
    nuggets = normalize_nuggets(analysis.get("nuggets"), words, duration)
    if not nuggets:
        nuggets = candidate_nuggets(words, duration, limit=max_nuggets)
    segments = [(float(item["start"]), float(item["end"])) for item in nuggets[:max_nuggets]]
    if not segments:
        return content_video, [], 0.0, []

    teaser = job_dir / "intro_teaser.mp4"
    render_segments(content_video, teaser, segments, "intro-teaser")
    teaser_duration = ffprobe_duration(teaser)
    teaser_words = words_for_segments(words, segments)
    save_json(job_dir / "intro_nuggets.json", {
        "nuggets": nuggets[:max_nuggets],
        "segments": segments,
        "duration": teaser_duration,
        "source": analysis.get("source", "unknown"),
    })
    write_status(intro_teaser=str(teaser), intro_duration=round(teaser_duration, 2), intro_nuggets=len(segments))
    return teaser, teaser_words, teaser_duration, nuggets[:max_nuggets]


def concat_intro_and_main(teaser: Path, main_video: Path, output_path: Path, job_dir: Path) -> None:
    list_path = job_dir / "concat_intro_main.txt"
    list_path.write_text(
        f"file '{str(teaser).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
        f"file '{str(main_video).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n",
        encoding="utf-8",
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
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
        "concat-intro-main",
    )


def generate_test_audio_assets(asset_dir: Path) -> dict[str, Any]:
    asset_dir.mkdir(parents=True, exist_ok=True)
    chime = asset_dir / "intro_chime_test.wav"
    whoosh = asset_dir / "transition_whoosh_test.wav"
    music = asset_dir / "music_bed_test.wav"

    if not chime.exists():
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1174:duration=0.28",
                "-af",
                "afade=t=out:st=0.20:d=0.08,volume=0.35",
                str(chime),
            ],
            "generate-intro-chime",
        )
    if not whoosh.exists():
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=pink:duration=0.55",
                "-af",
                "highpass=f=450,lowpass=f=4200,afade=t=in:st=0:d=0.08,afade=t=out:st=0.36:d=0.18,volume=0.22",
                str(whoosh),
            ],
            "generate-whoosh",
        )
    if not music.exists():
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=110:duration=180",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:duration=180",
                "-filter_complex",
                "[0:a]volume=0.025[a0];[1:a]volume=0.018[a1];[a0][a1]amix=inputs=2:duration=longest,afade=t=in:st=0:d=0.4",
                str(music),
            ],
            "generate-music-bed",
        )

    manifest = {
        "mode": "generated_test_assets",
        "asset_dir": str(asset_dir),
        "intro_chime": str(chime),
        "transition_whoosh": str(whoosh),
        "music_bed": str(music),
        "note": "Original generated placeholders for architecture testing. Replace with licensed branded assets later.",
        "candidate_sources": {
            "intro_chime": "https://pixabay.com/sound-effects/notification-6175/",
            "transition_whoosh": "https://pixabay.com/sound-effects/long-whoosh-194554/",
            "music_bed": "https://pixabay.com/music/pulses-corporate-tech-loop-197118/",
            "license_faq": "https://pixabay.com/service/faq/",
        },
    }
    return manifest


def resolve_audio_assets(job_dir: Path) -> dict[str, Any]:
    AUDIO_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    configured = {
        "intro_chime": Path(os.getenv("INTRO_CHIME_PATH", str(cfg("audio", "intro_chime", AUDIO_ASSET_DIR / "intro_chime.wav")))),
        "transition_whoosh": Path(os.getenv("TRANSITION_WHOOSH_PATH", str(cfg("audio", "transition_whoosh", AUDIO_ASSET_DIR / "transition_whoosh.wav")))),
        "music_bed": Path(os.getenv("MUSIC_BED_PATH", str(cfg("audio", "music_bed", AUDIO_ASSET_DIR / "music_bed.wav")))),
    }
    if all(path.exists() for path in configured.values()):
        manifest = {
            "mode": "configured_assets",
            "asset_dir": str(AUDIO_ASSET_DIR),
            **{key: str(path) for key, path in configured.items()},
            "note": "Using configured audio assets.",
        }
    else:
        manifest = generate_test_audio_assets(AUDIO_ASSET_DIR)
    save_json(job_dir / "asset_manifest.json", manifest)
    write_status(audio_assets=manifest["mode"])
    return manifest


def mix_audio_bed(input_path: Path, output_path: Path, assets: dict[str, Any], teaser_duration: float) -> None:
    chime = Path(assets.get("intro_chime", ""))
    whoosh = Path(assets.get("transition_whoosh", ""))
    music = Path(assets.get("music_bed", ""))
    if not chime.exists() or not whoosh.exists() or not music.exists():
        shutil.copy2(input_path, output_path)
        write_status(audio_mix="skipped_missing_assets")
        return

    duration = ffprobe_duration(input_path)
    whoosh_ms = max(0, int(max(0.0, teaser_duration - 0.18) * 1000))
    chime_volume = cfg_float("audio", "chime_volume", 0.26)
    whoosh_volume = cfg_float("audio", "whoosh_volume", 0.20)
    music_volume = cfg_float("audio", "music_volume", 0.055)
    ducking_threshold = cfg_float("audio", "ducking_threshold", 0.03)
    ducking_ratio = cfg_float("audio", "ducking_ratio", 8.0)
    filter_complex = (
        "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=1.0[voice];"
        f"[3:a]atrim=0:{duration:.3f},aformat=sample_rates=48000:channel_layouts=stereo,volume={music_volume}[music];"
        f"[music][voice]sidechaincompress=threshold={ducking_threshold}:ratio={ducking_ratio}:attack=20:release=350[ducked];"
        f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay=0|0,volume={chime_volume}[chime];"
        f"[2:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay={whoosh_ms}|{whoosh_ms},volume={whoosh_volume}[whoosh];"
        "[voice][ducked][chime][whoosh]amix=inputs=4:duration=first:dropout_transition=0,alimiter=limit=0.95[aout]"
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-i",
            str(chime),
            "-i",
            str(whoosh),
            "-stream_loop",
            "-1",
            "-i",
            str(music),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        "audio-mix",
    )
    write_status(audio_mix="complete")


def resource_snapshot(start_time: float) -> dict[str, Any]:
    elapsed = round(time.perf_counter() - start_time, 3)
    snapshot: dict[str, Any] = {
        "elapsed_seconds": elapsed,
        "stage_timings": STAGE_TIMINGS,
    }
    try:
        import resource  # type: ignore

        usage = resource.getrusage(resource.RUSAGE_SELF)
        snapshot["process_user_cpu_seconds"] = round(float(usage.ru_utime), 3)
        snapshot["process_system_cpu_seconds"] = round(float(usage.ru_stime), 3)
        snapshot["process_max_rss_mb"] = round(float(usage.ru_maxrss) / 1024, 1)
    except Exception as exc:
        snapshot["resource_error"] = str(exc)[:200]
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        snapshot["system_memory_total_mb"] = round(vm.total / (1024 * 1024), 1)
        snapshot["system_memory_available_mb"] = round(vm.available / (1024 * 1024), 1)
        snapshot["system_cpu_count"] = psutil.cpu_count()
        snapshot["system_cpu_percent_sample"] = psutil.cpu_percent(interval=0.2)
    except Exception as exc:
        snapshot["psutil_error"] = str(exc)[:200]
    return snapshot


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


def upload_to_youtube(video_path: Path, thumbnail_path: Path, metadata: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    status_path = job_dir / "upload_status.json"
    enabled = env_bool("YOUTUBE_UPLOAD_ENABLED", cfg_bool("youtube", "upload_enabled", True))
    if not enabled:
        status = {"state": "skipped", "reason": "YOUTUBE_UPLOAD_ENABLED=false"}
        save_json(status_path, status)
        write_status(youtube_status=status["state"], youtube_reason=status["reason"])
        return status

    title = str(metadata.get("title", "")).strip()
    description = str(metadata.get("description", "")).strip()
    if not title or not description:
        status = {"state": "blocked", "reason": "metadata title/description missing"}
        save_json(status_path, status)
        write_status(youtube_status=status["state"], youtube_reason=status["reason"])
        return status

    if not YOUTUBE_TOKEN_PATH.exists():
        status = {
            "state": "skipped",
            "reason": f"YouTube token missing: {YOUTUBE_TOKEN_PATH}",
            "needed": "Create OAuth token.json with youtube.upload scope and mount it into assets/youtube.",
        }
        save_json(status_path, status)
        write_status(youtube_status=status["state"], youtube_reason=status["reason"])
        return status

    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except Exception as exc:
        status = {
            "state": "skipped",
            "reason": f"YouTube Python dependencies missing: {exc}",
        }
        save_json(status_path, status)
        write_status(youtube_status=status["state"], youtube_reason=status["reason"])
        return status

    write_status(step="youtube-upload", youtube_status="running")
    try:
        scopes = ["https://www.googleapis.com/auth/youtube.upload"]
        credentials = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_PATH), scopes=scopes)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            YOUTUBE_TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials.valid:
            raise RuntimeError("YouTube OAuth credentials are not valid")

        youtube = build("youtube", "v3", credentials=credentials)
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": metadata.get("tags", [])[:15],
                "categoryId": str(os.getenv("YOUTUBE_CATEGORY_ID", str(cfg("youtube", "category_id", "27")))),
            },
            "status": {
                "privacyStatus": os.getenv("YOUTUBE_PRIVACY_STATUS", str(cfg("youtube", "privacy_status", "public"))),
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _status, response = request.next_chunk()
        video_id = response["id"]

        thumb_state = "skipped"
        if thumbnail_path.exists():
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
            ).execute()
            thumb_state = "uploaded"

        status = {
            "state": "uploaded",
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "privacy_status": body["status"]["privacyStatus"],
            "thumbnail": thumb_state,
        }
        save_json(status_path, status)
        write_status(youtube_status=status["state"], youtube_url=status["url"], youtube_privacy=status["privacy_status"])
        return status
    except Exception as exc:
        status = {"state": "failed", "reason": str(exc)[:1000]}
        save_json(status_path, status)
        write_status(youtube_status=status["state"], youtube_reason=status["reason"])
        return status


def main() -> int:
    render_started = time.perf_counter()
    write_status(
        state="running",
        started_at=utc_now(),
        upload_dir=str(UPLOAD_DIR),
        output_root=str(OUTPUT_ROOT),
        pipeline_dir=str(PIPELINE_DIR),
        pipeline_config=str(PIPELINE_CONFIG_PATH),
    )
    try:
        source, probe = newest_valid_upload()
        job_dir = make_job_dir(source)
        log_path = job_dir / "render.log"
        write_status(state="running", input=str(source), job_dir=str(job_dir), log=str(log_path))

        raw_input = job_dir / f"raw_input{source.suffix.lower()}"
        normalized = job_dir / "normalized.mp4"
        edited = job_dir / "edited_base.mp4"
        assembled = job_dir / "assembled_intro_main.mp4"
        mixed = job_dir / "with_audio_bed.mp4"
        final = job_dir / "final.mp4"
        thumbnail = job_dir / "thumbnail.jpg"

        shutil.copy2(source, raw_input)
        save_json(job_dir / "input_probe.json", probe)
        save_json(job_dir / "editing_preset.json", EDITING_PRESET)
        save_json(job_dir / "pipeline_config_snapshot.json", PIPELINE_CONFIG)

        normalize_video(raw_input, normalized)
        cut_count = cut_silences(normalized, edited, job_dir)
        captions, words = transcribe_and_write_captions(edited, job_dir)
        content_video = edited
        content_words = words

        word_gap_config = EDITING_PRESET.get("word_gap_fallback", {})
        if cut_count == 0 and words and bool(word_gap_config.get("enabled", True)):
            duration_for_word_cuts = ffprobe_duration(edited)
            gap_segments = word_gap_segments(
                words,
                duration_for_word_cuts,
                max_gap=float(word_gap_config.get("max_gap_seconds", 0.22)),
                padding=float(word_gap_config.get("padding_seconds", 0.035)),
            )
            save_json(job_dir / "word_gap_segments.json", {
                "duration": duration_for_word_cuts,
                "segments": gap_segments,
                "cut_count": max(0, len(gap_segments) - 1),
            })
            if len(gap_segments) > 1:
                word_gap_cut = job_dir / "word_gap_cut.mp4"
                cut_count = render_segments(edited, word_gap_cut, gap_segments, "word-gap-cuts")
                remapped_words = remap_words_to_segments(words, gap_segments)
                save_json(job_dir / "words_remapped.json", remapped_words)
                content_video = word_gap_cut
                content_words = remapped_words
                write_status(word_gap_cut=str(word_gap_cut), cut_count=cut_count)

        content_duration = ffprobe_duration(content_video)
        analysis = analyze_transcript(content_words, content_duration, job_dir)
        save_json(job_dir / "title_description.json", {
            "title": analysis.get("title", ""),
            "description": analysis.get("description", ""),
            "tags": analysis.get("tags", []),
            "hook": analysis.get("hook", ""),
            "source": analysis.get("source", "unknown"),
        })
        save_json(job_dir / "transcript_analysis.json", analysis)

        teaser, teaser_words, teaser_duration, nuggets = build_intro_teaser(content_video, content_words, analysis, job_dir)
        if teaser_words and teaser != content_video:
            concat_intro_and_main(teaser, content_video, assembled, job_dir)
            final_caption_words = teaser_words + shift_words(content_words, teaser_duration)
        else:
            shutil.copy2(content_video, assembled)
            teaser_duration = 0.0
            final_caption_words = content_words

        assets = resolve_audio_assets(job_dir)
        mix_audio_bed(assembled, mixed, assets, teaser_duration)
        captions = write_ass_captions(final_caption_words, job_dir)
        burn_captions_or_copy(mixed, final, captions)
        make_thumbnail(final, thumbnail)

        final_duration = ffprobe_duration(final)
        youtube_metadata = {
            "title": analysis.get("title", ""),
            "description": analysis.get("description", ""),
            "tags": analysis.get("tags", []),
        }
        upload_status = upload_to_youtube(final, thumbnail, youtube_metadata, job_dir)
        resources = resource_snapshot(render_started)
        save_json(job_dir / "resource_usage.json", resources)
        metadata = {
            "privacy_status": os.getenv("YOUTUBE_PRIVACY_STATUS", str(cfg("youtube", "privacy_status", "public"))),
            "style": "tight jump cuts, intro teaser, ducked music bed, bold burned-in short-form captions",
            "editing_preset": EDITING_PRESET["name"],
            "pipeline_dir": str(PIPELINE_DIR),
            "pipeline_config": str(PIPELINE_CONFIG_PATH),
            "source": str(source),
            "job_dir": str(job_dir),
            "final": str(final),
            "thumbnail": str(thumbnail),
            "title": youtube_metadata["title"],
            "description": youtube_metadata["description"],
            "tags": youtube_metadata["tags"],
            "intro_nuggets": nuggets,
            "duration": final_duration,
            "cut_count": cut_count,
            "caption_count": len(build_caption_groups(final_caption_words)),
            "asset_manifest": str(job_dir / "asset_manifest.json"),
            "llm_usage": str(job_dir / "llm_usage.json"),
            "upload_status": upload_status,
            "resource_usage": resources,
            "completed_at": utc_now(),
        }
        save_json(job_dir / "metadata.json", metadata)
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
            intro_duration=round(teaser_duration, 2),
            youtube_status=upload_status.get("state"),
            youtube_url=upload_status.get("url", ""),
            elapsed_seconds=resources.get("elapsed_seconds"),
            max_rss_mb=resources.get("process_max_rss_mb"),
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
