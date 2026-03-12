import os
import json
import shutil
import subprocess
import tempfile

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# =========================
# Config
# =========================
TARGET_W = 1080
TARGET_H = 1920
MEDIA_DIR = os.getenv("MEDIA_DIR", "/media")

VF_916 = (
    f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
    f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2"
)


# =========================
# Basic endpoints
# =========================
@app.get("/")
def root():
    return "ffmpeg-renderer up", 200


@app.get("/health")
def health():
    return "ok", 200


# =========================
# Local storage endpoints
# =========================
@app.post("/save")
def save_file():
    """
    로컬 볼륨에 파일을 저장합니다.
    Query param: path (상대경로, 예: ID(Production)/audio.mp3)
    Body: 저장할 바이너리 데이터
    """
    rel_path = request.args.get("path", "").strip()
    if not rel_path:
        return jsonify({"ok": False, "error": "path query param is required"}), 400

    content_type = request.content_type or "application/octet-stream"
    abs_path = os.path.join(MEDIA_DIR, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    data = request.get_data()
    with open(abs_path, "wb") as f:
        f.write(data)

    return jsonify({
        "ok": True,
        "name": rel_path,
        "contentType": content_type,
        "size": len(data),
    }), 200


@app.get("/file")
def get_file():
    """
    로컬 볼륨에서 파일을 서빙합니다.
    Query param: path (상대경로 또는 절대경로)
    """
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"ok": False, "error": "path query param is required"}), 400

    if not os.path.isabs(path):
        path = os.path.join(MEDIA_DIR, path)

    if not os.path.exists(path):
        return jsonify({"ok": False, "error": f"file not found: {path}"}), 404

    return send_file(path, mimetype="video/mp4")


# =========================
# Helpers
# =========================
def run_cmd(cmd: list[str]):
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "Command failed:\n"
            f"{' '.join(cmd)}\n\n"
            f"OUTPUT:\n{e.output}"
        ) from e


def parse_bool(v, default=True):
    """
    n8n에서 true/false가 문자열로 들어와도 안전하게 처리
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(v)


def ffprobe_duration_sec(path: str) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ], text=True).strip()
    return float(out)


def normalize_scene_veo(video_in: str, video_out: str, target_sec: float, fps: int) -> float:
    """
    VEO 모드 씬 mp4 처리:
    - target_sec에 맞춰 Trim 또는 Pad/Clone 또는 Loop
    - 9:16 + fps 통일
    """
    actual = ffprobe_duration_sec(video_in)
    vf_base = f"{VF_916},fps={fps}"

    tol = 0.03
    small_pad_sec = 0.5

    # 길거나 거의 같으면 Trim
    if abs(actual - target_sec) <= tol or actual > target_sec:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            video_out
        ])
        return target_sec

    # 짧으면 Pad / Loop
    short_by = target_sec - actual

    if short_by <= small_pad_sec:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", f"{vf_base},tpad=stop_mode=clone:stop_duration={short_by:.3f}",
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            video_out
        ])
        return target_sec

    # 기존 VEO 호환용 loop 유지
    run_cmd([
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", video_in,
        "-vf", vf_base,
        "-t", f"{target_sec:.3f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-an",
        video_out
    ])
    return target_sec


def normalize_scene_grok(
    video_in: str,
    video_out: str,
    target_sec: float,
    fps: int,
    is_last: bool
) -> float:
    """
    GROK 모드 전용:
    - 입력 영상은 6초짜리라고 가정
    - 모든 target_sec는 6.0 이하여야 함
    - 마지막 씬이 아니면 target_sec만큼 trim
    - 마지막 씬이면 영상 원본을 최대 6초까지 전부 사용
    - loop 절대 사용 안 함
    """
    actual = ffprobe_duration_sec(video_in)
    vf_base = f"{VF_916},fps={fps}"

    if actual <= 0:
        raise RuntimeError("Invalid grok input video duration")

    # GROK 모드는 모든 duration이 6초 이하여야 함
    if target_sec > 6.0:
        raise RuntimeError(
            f"Grok mode requires every duration <= 6.0 sec, got {target_sec:.3f}"
        )

    # 마지막 씬: 영상 원본을 끝까지 사용 (최대 6초)
    if is_last:
        final_sec = min(actual, 6.0)
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-t", f"{final_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            video_out
        ])
        return final_sec

    # 일반 씬: target_sec만큼 trim
    run_cmd([
        "ffmpeg", "-y", "-i", video_in,
        "-vf", vf_base,
        "-t", f"{target_sec:.3f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-an",
        video_out
    ])
    return target_sec


def cut_audio_segment_to_aac(
    audio_in: str,
    audio_out: str,
    start_sec: float,
    dur_sec: float,
    pad_to_sec: float | None = None
):
    """
    audio_in에서 start_sec부터 dur_sec만큼 잘라 AAC(m4a)로 저장.
    - pad_to_sec가 주어지면, apad로 무음 패딩 후 길이 고정.
    - 정밀도 위해 -ss를 -i 뒤에 둠
    """
    if dur_sec <= 0:
        dur_sec = 0.001

    if pad_to_sec is None:
        run_cmd([
            "ffmpeg", "-y",
            "-i", audio_in,
            "-ss", f"{start_sec:.3f}",
            "-t", f"{dur_sec:.3f}",
            "-c:a", "aac",
            "-b:a", "192k",
            audio_out
        ])
        return

    tail = max(0.0, pad_to_sec - dur_sec)
    run_cmd([
        "ffmpeg", "-y",
        "-i", audio_in,
        "-ss", f"{start_sec:.3f}",
        "-t", f"{dur_sec:.3f}",
        "-af", f"apad=pad_dur={tail:.3f}",
        "-t", f"{pad_to_sec:.3f}",
        "-c:a", "aac",
        "-b:a", "192k",
        audio_out
    ])


def pad_video_tail(video_in: str, video_out: str, extra_sec: float, fps: int) -> float:
    """
    video_in 뒤에 extra_sec 만큼 마지막 프레임 복제(tpad)로 여운 추가.
    """
    if extra_sec <= 0:
        run_cmd([
            "ffmpeg", "-y",
            "-i", video_in,
            "-c", "copy",
            video_out
        ])
        return ffprobe_duration_sec(video_out)

    run_cmd([
        "ffmpeg", "-y",
        "-i", video_in,
        "-vf", f"fps={fps},tpad=stop_mode=clone:stop_duration={extra_sec:.3f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-an",
        video_out
    ])
    return ffprobe_duration_sec(video_out)


def mux_video_audio(video_in: str, audio_in: str, out_mp4: str):
    """
    비디오는 이미 앞단에서 fps/해상도 정리 끝났다고 가정.
    여기서는 비디오를 copy하고 오디오만 AAC로 mux.
    -r 제거
    """
    run_cmd([
        "ffmpeg", "-y",
        "-i", video_in,
        "-i", audio_in,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        out_mp4
    ])


# =========================
# Render endpoint
# =========================
@app.post("/render")
def render():
    try:
        data = request.get_json(force=True, silent=True)
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        mode = str(data.get("mode", "veo")).strip().lower()
        if mode not in ("veo", "grok"):
            return jsonify({
                "ok": False,
                "error": "mode must be 'veo' or 'grok'"
            }), 400

        audio = data.get("audio")
        videos = data.get("videos")
        output = data.get("output")
        durations_sec = data.get("durations_sec")
        fps = int(data.get("fps", 30))

        # VEO/GROK 공통 마지막 씬 여운
        tail_extra_sec = float(data.get("tail_extra_sec", 2.0))

        # VEO 모드용 옵션
        last_audio_take_rest = parse_bool(data.get("last_audio_take_rest", True))

        # 하위호환
        if durations_sec is None:
            durations_ms = data.get("durations_ms")
            if durations_ms is not None:
                durations_sec = [float(x) / 1000.0 for x in durations_ms]

        if not audio or not videos or not output:
            return jsonify({"ok": False, "error": "payload missing fields"}), 400
        if not isinstance(videos, list):
            return jsonify({"ok": False, "error": "videos must be array"}), 400
        if durations_sec is None:
            return jsonify({
                "ok": False,
                "error": "payload missing durations_sec (or durations_ms)"
            }), 400
        if len(durations_sec) != len(videos):
            return jsonify({"ok": False, "error": "length mismatch"}), 400

        durations_sec = [float(x) for x in durations_sec]

        # GROK 모드: 모든 duration은 무조건 6초 이하
        if mode == "grok":
            for i, d in enumerate(durations_sec):
                if d > 6.0:
                    return jsonify({
                        "ok": False,
                        "error": f"grok mode requires every duration <= 6.0 sec (index={i}, value={d:.3f})"
                    }), 400

        # 출력 디렉터리 생성
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) 오디오 경로 (로컬 파일 직접 사용)
            audio_path = audio
            audio_total_sec = ffprobe_duration_sec(audio_path)

            seg_paths = []
            debug_scenes = []

            cur_start = 0.0
            sum_script = sum(durations_sec)

            for i, (video_path, dur) in enumerate(zip(videos, durations_sec)):
                target_sec = float(dur)
                is_last = (i == len(videos) - 1)

                raw_vp = video_path  # 로컬 파일 직접 사용
                fixed_vp = os.path.join(tmpdir, f"video_fixed_{i}.mp4")

                # 2) 비디오 길이 처리
                if mode == "grok":
                    video_sec = normalize_scene_grok(
                        video_in=raw_vp,
                        video_out=fixed_vp,
                        target_sec=target_sec,
                        fps=fps,
                        is_last=is_last
                    )
                else:
                    video_sec = normalize_scene_veo(
                        video_in=raw_vp,
                        video_out=fixed_vp,
                        target_sec=target_sec,
                        fps=fps
                    )

                # 2-1) 마지막 씬 여운 추가 (VEO / GROK 공통)
                if is_last and tail_extra_sec > 0:
                    fixed_tail = os.path.join(tmpdir, f"video_fixed_tail_{i}.mp4")
                    video_sec = pad_video_tail(fixed_vp, fixed_tail, tail_extra_sec, fps)
                    fixed_vp = fixed_tail

                # 3) 오디오 구간 계산
                remaining = max(0.0, audio_total_sec - cur_start)
                audio_seg = os.path.join(tmpdir, f"audio_seg_{i}.m4a")

                if mode == "grok":
                    if is_last:
                        audio_seg_sec = min(remaining, video_sec)
                        cut_audio_segment_to_aac(
                            audio_in=audio_path,
                            audio_out=audio_seg,
                            start_sec=cur_start,
                            dur_sec=audio_seg_sec,
                            pad_to_sec=video_sec
                        )
                        note = "Grok last: use full last video + tail; audio padded to full video if needed"
                    else:
                        audio_seg_sec = min(target_sec, remaining)
                        cut_audio_segment_to_aac(
                            audio_in=audio_path,
                            audio_out=audio_seg,
                            start_sec=cur_start,
                            dur_sec=audio_seg_sec,
                            pad_to_sec=None
                        )
                        note = "Grok normal: trim video/audio to target"
                else:
                    # VEO 모드 기존 동작 유지
                    if is_last and last_audio_take_rest:
                        audio_seg_sec = remaining
                    else:
                        audio_seg_sec = min(target_sec, remaining)

                    if is_last:
                        cut_audio_segment_to_aac(
                            audio_in=audio_path,
                            audio_out=audio_seg,
                            start_sec=cur_start,
                            dur_sec=audio_seg_sec,
                            pad_to_sec=video_sec
                        )
                        note = "Veo last: took rest audio (if enabled); padded to video with silence"
                    else:
                        cut_audio_segment_to_aac(
                            audio_in=audio_path,
                            audio_out=audio_seg,
                            start_sec=cur_start,
                            dur_sec=audio_seg_sec,
                            pad_to_sec=None
                        )
                        note = "Veo normal: cut audio to target"

                # 4) 씬별 mux
                seg_out = os.path.join(tmpdir, f"seg_{i}.mp4")
                mux_video_audio(fixed_vp, audio_seg, seg_out)
                seg_paths.append(seg_out)

                debug_scenes.append({
                    "idx": i,
                    "mode": mode,
                    "is_last": is_last,
                    "start_sec": round(cur_start, 3),
                    "target_script_sec": round(target_sec, 3),
                    "audio_cut_sec": round(audio_seg_sec, 3),
                    "video_final_sec": round(video_sec, 3),
                    "tail_extra_sec": round(tail_extra_sec, 3) if is_last else 0.0,
                    "note": note
                })

                cur_start += target_sec

            # 5) Concat segments
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for vp in seg_paths:
                    f.write(f"file '{vp}'\n")

            final_video = os.path.join(tmpdir, "final.mp4")
            run_cmd([
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                final_video
            ])

            # 6) 로컬 출력 경로에 복사 (GCS 업로드 대신)
            shutil.copy2(final_video, output)

        return jsonify({
            "ok": True,
            "mode": mode,
            "output": output,
            "videoCount": len(videos),
            "audio_total_sec": round(audio_total_sec, 3),
            "sum_script_sec": round(sum_script, 3),
            "tail_extra_sec": round(tail_extra_sec, 3),
            "last_audio_take_rest": last_audio_take_rest if mode == "veo" else False,
            "debug": debug_scenes
        }), 200

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "internal error",
            "detail": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
