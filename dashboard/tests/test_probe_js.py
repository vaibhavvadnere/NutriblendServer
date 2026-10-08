"""The browser's MP4 reader (components/browser_upload/probe.js) on real files.

Needs node and ffmpeg; skipped without them. Files are generated with ffmpeg, and the
reader's answers (length, codec, size, fast-start) are compared with ffprobe's.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
PROBE_JS = HERE.parent / "dashboard" / "components" / "browser_upload" / "probe.js"
RUNNER = HERE / "js" / "run_probe.mjs"

pytestmark = pytest.mark.skipif(not (shutil.which("node") and shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="needs node, ffmpeg and ffprobe")


def _ffmpeg(out: Path, *args: str, audio: bool = True) -> Path:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    subprocess.run([*cmd, *args, str(out)], check=True, capture_output=True)
    return out


@pytest.fixture(scope="module")
def files(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("videos")
    h264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    f = {
        "good": _ffmpeg(d / "good.mp4", *h264, "-movflags", "+faststart"),
        "slow": _ffmpeg(d / "slow.mp4", *h264),                                   # index (moov) at the end
        "mpeg4": _ffmpeg(d / "mpeg4.mp4", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-movflags", "+faststart", audio=False),
        "hevc": _ffmpeg(d / "hevc.mp4", "-c:v", "libx265", "-pix_fmt", "yuv420p", "-x265-params", "log-level=none",
                        "-movflags", "+faststart", audio=False),
        "mp3": _ffmpeg(d / "mp3.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "libmp3lame", "-movflags", "+faststart"),
        "silent": _ffmpeg(d / "silent.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", audio=False),
        "frag": _ffmpeg(d / "frag.mp4", *h264, "-movflags", "frag_keyframe+empty_moov"),
    }
    try:
        f["tenbit"] = _ffmpeg(d / "tenbit.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p10le", "-movflags", "+faststart", audio=False)
    except subprocess.CalledProcessError:
        pass                                                                      # this ffmpeg has no 10-bit x264
    good = f["good"].read_bytes()
    (d / "cut.mp4").write_bytes(good[:20000])                                    # download interrupted
    (d / "random.mp4").write_bytes(bytes(range(256)) * 20)                       # not a video at all
    (d / "no_moov.mp4").write_bytes(good[: good.index(b"moov") - 4])             # recording never finished
    rot = bytearray(good)                                                         # phone video recorded upright
    m = rot.index(b"tkhd") + 4 + 40                                               # the track's 3x3 matrix
    rot[m:m + 4] = struct.pack(">i", 0)
    rot[m + 4:m + 8] = struct.pack(">i", 0x10000)
    rot[m + 12:m + 16] = struct.pack(">i", -0x10000)
    rot[m + 16:m + 20] = struct.pack(">i", 0)
    (d / "rot.mp4").write_bytes(bytes(rot))
    for name in ("cut", "random", "no_moov", "rot"):
        f[name] = d / f"{name}.mp4"
    return f


@pytest.fixture(scope="module")
def probes(files) -> dict:
    run = subprocess.run(["node", str(RUNNER), str(PROBE_JS), *map(str, files.values())],
                         check=True, capture_output=True, text=True, timeout=60)
    return json.loads(run.stdout)


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,profile,width,height",
                          "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_good_file_reads_like_ffprobe(files, probes):
    p = probes["good.mp4"]
    truth = _ffprobe(files["good"])
    assert p["ok"] and p["codes"] == []
    assert p["videoCodec"] == "H.264" and p["audioCodec"] == "AAC" and p["videoProfile"] == "High"
    assert (p["width"], p["height"]) == (320, 240)
    assert p["duration"] == pytest.approx(float(truth["format"]["duration"]), abs=0.05)
    assert p["fastStart"] is True
    assert p["summary"] == "H.264 High · 320×240 · 0:03 · fast-start"


def test_index_at_the_end_is_flagged_slow_start(probes):
    p = probes["slow.mp4"]
    assert p["fastStart"] is False and p["codes"] == ["slow_start"]
    assert "faststart" in p["warnings"][0]["text"]            # tells the admin the fix
    assert p["videoCodec"] == "H.264" and p["duration"] == pytest.approx(3.0, abs=0.05)   # still read in full


def test_other_codecs_are_flagged(probes):
    assert "codec" in probes["mpeg4.mp4"]["codes"] and probes["mpeg4.mp4"]["videoCodec"] == "MPEG-4 Visual"
    assert "codec" in probes["hevc.mp4"]["codes"] and probes["hevc.mp4"]["videoCodec"] == "HEVC (H.265)"
    assert "codec" not in probes["good.mp4"]["codes"]


def test_ten_bit_h264_is_flagged(files, probes):
    if "tenbit" not in files:
        pytest.skip("no 10-bit x264 in this ffmpeg")
    p = probes["tenbit.mp4"]
    assert p["videoProfile"] == "High 10" and "profile" in p["codes"]


def test_audio_checks(probes):
    assert "audio_codec" in probes["mp3.mp4"]["codes"] and probes["mp3.mp4"]["audioCodec"] == "MP3"
    silent = probes["silent.mp4"]
    assert silent["codes"] == ["no_audio"] and silent["warnings"][0]["level"] == "info"


def test_fragmented_mp4_is_fine_without_a_length(probes):
    p = probes["frag.mp4"]
    assert p["ok"] and p["fragmented"] and p["fastStart"] is True
    assert p["videoCodec"] == "H.264" and "no_duration" not in p["codes"]


def test_portrait_phone_video_swaps_width_and_height(probes):
    assert (probes["rot.mp4"]["width"], probes["rot.mp4"]["height"]) == (240, 320)


@pytest.mark.parametrize("name,code", [("cut.mp4", "truncated"), ("random.mp4", "not_mp4"), ("no_moov.mp4", "no_index")])
def test_broken_files_are_errors(probes, name, code):
    p = probes[name]
    assert code in p["codes"]
    assert [w["level"] for w in p["warnings"] if w["code"] == code] == ["error"]
