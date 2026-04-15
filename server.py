from flask import Flask, request, jsonify, send_file
import os, requests, threading, subprocess

app = Flask(__name__)
BASE = "/tmp/ai_video/"
os.makedirs(BASE, exist_ok=True)

# Status track karo
status_map = {}

def get_duration(file_path):
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ], capture_output=True, text=True)
        return float(result.stdout.strip())
    except:
        return None

def process_all(scenes, job_id):
    status_map[job_id] = "processing"
    scene_videos = []

    for scene in scenes:
        n = scene.get("scene_no")
        print(f"\n SCENE {n} SHURU...")

        # ── TTS ──
        narration  = scene.get("narration_text","")
        audio_path = f"{BASE}{job_id}_scene{n}_audio.mp3"
        safe_narr  = narration.replace('"', '\\"')

        os.system(
            f'edge-tts --text "{safe_narr}" '
            f'--voice en-US-GuyNeural '
            f'--write-media "{audio_path}"'
        )

        actual_dur = get_duration(audio_path) \
            or scene.get("duration_seconds", 15)

        # ── IMAGES ──
        imgs = []
        for i in range(1, 5):
            url = scene.get(f"image_{i}_url")
            if url:
                path = f"{BASE}{job_id}_scene{n}_img{i}.jpg"
                try:
                    r = requests.get(url, timeout=30)
                    open(path,"wb").write(r.content)
                    imgs.append(path)
                except:
                    pass

        # ── VIDEO ──
        video_url  = scene.get("video_url")
        video_path = f"{BASE}{job_id}_scene{n}_vid.mp4"
        has_video  = False
        if video_url:
            try:
                r = requests.get(video_url, timeout=60)
                open(video_path,"wb").write(r.content)
                has_video = True
            except:
                pass

        # ── Duration Split ──
        each_img = round(
            (actual_dur * 0.60) / len(imgs), 2
        ) if imgs else 3.0
        vid_time = round(actual_dur * 0.40, 2)

        # ── FFmpeg ──
        scene_out = f"{BASE}{job_id}_scene{n}.mp4"
        inputs    = ""
        idx       = 0

        for img in imgs:
            inputs += f'-loop 1 -t {each_img} -i "{img}" '
            idx    += 1

        if has_video:
            inputs += f'-t {vid_time} -i "{video_path}" '
            idx    += 1

        inputs    += f'-i "{audio_path}" '
        audio_idx  = idx
        total_clips = idx

        # Scale + Ken Burns
        scale_parts   = ""
        scaled_labels = ""

        for i in range(len(imgs)):
            zoom = (
                f'zoompan=z=\'min(zoom+0.001,1.3)\':'
                f'x=\'iw/2-(iw/zoom/2)\':'
                f'y=\'ih/2-(ih/zoom/2)\':'
                f'd={int(each_img*25)}:s=1920x1080,'
            ) if i % 2 == 0 else (
                f'zoompan=z=\'if(lte(zoom,1.0),1.3,'
                f'max(1.001,zoom-0.001))\':'
                f'x=\'iw/2-(iw/zoom/2)\':'
                f'y=\'ih/2-(ih/zoom/2)\':'
                f'd={int(each_img*25)}:s=1920x1080,'
            )
            scale_parts += (
                f'[{i}:v]scale=1920:1080:'
                f'force_original_aspect_ratio=decrease,'
                f'pad=1920:1080:(ow-iw)/2:(oh-ih)/2,'
                f'setsar=1,fps=25,{zoom}'
                f'setpts=PTS-STARTPTS[v{i}];'
            )
            scaled_labels += f'[v{i}]'

        if has_video:
            vi = len(imgs)
            scale_parts += (
                f'[{vi}:v]scale=1920:1080:'
                f'force_original_aspect_ratio=decrease,'
                f'pad=1920:1080:(ow-iw)/2:(oh-ih)/2,'
                f'setsar=1,fps=25,'
                f'setpts=PTS-STARTPTS[v{vi}];'
            )
            scaled_labels += f'[v{vi}]'

        # Transitions
        all_labels   = [f'v{i}' for i in range(total_clips)]
        trans_filter = ""
        prev_label   = all_labels[0]

        for i in range(1, total_clips):
            out_label     = f'out{i}'
            trans_filter += (
                f'[{prev_label}][{all_labels[i]}]'
                f'xfade=transition=fade:duration=0.5:'
                f'offset={each_img*i - 0.5}[{out_label}];'
            )
            prev_label = out_label

        concat = (
            f'{scale_parts}{trans_filter}'
            f'[{prev_label}]null[outv]'
        )

        ffmpeg_cmd = (
            f'ffmpeg -y {inputs}'
            f'-filter_complex "{concat}" '
            f'-map "[outv]" -map {audio_idx}:a '
            f'-c:v libx264 -preset fast -crf 23 '
            f'-c:a aac -ar 44100 -b:a 192k '
            f'-shortest -movflags +faststart '
            f'"{scene_out}"'
        )

        ret = os.system(ffmpeg_cmd)
        if ret == 0 and os.path.exists(scene_out):
            print(f"  Scene {n} ✅")
            scene_videos.append((n, scene_out))

    # ── MERGE ──
    scene_videos.sort(key=lambda x: x[0])
    list_file = f"{BASE}{job_id}_list.txt"

    with open(list_file, "w") as f:
        for _, path in scene_videos:
            f.write(f"file '{path}'\n")

    final_out = f"{BASE}{job_id}_final.mp4"
    os.system(
        f'ffmpeg -y -f concat -safe 0 '
        f'-i "{list_file}" '
        f'-c:v libx264 -preset fast '
        f'-c:a aac -movflags +faststart '
        f'"{final_out}"'
    )

    if os.path.exists(final_out):
        status_map[job_id] = "ready"
        print(f"✅ Job {job_id} READY!")
    else:
        status_map[job_id] = "failed"


# ── ENDPOINT 1: Process Start ──
@app.route('/process-scenes', methods=['POST'])
def handle():
    import uuid
    data   = request.json
    job_id = str(uuid.uuid4())[:8]

    scenes = (
        data if isinstance(data, list)
        else data.get("scenes", [data])
    )
    scenes.sort(key=lambda x: x.get("scene_no", 0))

    status_map[job_id] = "processing"

    threading.Thread(
        target=process_all,
        args=(scenes, job_id)
    ).start()

    return jsonify({
        "status" : "started",
        "job_id" : job_id,
        "message": "Processing shuru, /status se check karo"
    })


# ── ENDPOINT 2: Status Check ──
@app.route('/status/<job_id>', methods=['GET'])
def check_status(job_id):
    s = status_map.get(job_id, "not_found")
    return jsonify({
        "job_id": job_id,
        "status": s
    })


# ── ENDPOINT 3: Video Download ──
@app.route('/get-video/<job_id>', methods=['GET'])
def get_video(job_id):
    final = f"{BASE}{job_id}_final.mp4"

    if not os.path.exists(final):
        return jsonify({
            "error": "Video ready nahi hai"
        }), 404

    return send_file(
        final,
        mimetype      = "video/mp4",
        as_attachment = True,
        download_name = "final_video.mp4"
    )


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "running"})


port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port)
