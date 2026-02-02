import os
import base64
import json
import tempfile
import subprocess
import uuid
import datetime as dt
import requests
from flask import Flask, request, jsonify
import boto3
from botocore.config import Config
import math
import random
from threading import Thread
import logging
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
MAX_DURATION = int(os.getenv('MAX_DURATION', '3600'))
MAX_CLIPS = int(os.getenv('MAX_CLIPS', '5'))  # Solo 5 clip per shorts!

app = Flask(__name__)

# R2 Config
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL")
R2_REGION = os.environ.get("R2_REGION", "auto")
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")

# Pexels / Pixabay API
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")

jobs = {}
MAX_JOBS = 50

def get_s3_client():
    """Client S3 per Cloudflare R2"""
    if R2_ACCOUNT_ID:
        endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    else:
        endpoint_url = None
    if endpoint_url is None:
        raise RuntimeError("R2_ACCOUNT_ID mancante")
    
    session = boto3.session.Session()
    s3_client = session.client(
        service_name="s3",
        region_name=R2_REGION,
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(s3={"addressing_style": "virtual"}),
    )
    return s3_client

def download_file(url: str) -> str:
    """Download file da URL"""
    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    for chunk in resp.iter_content(chunk_size=1024 * 1024):
        if chunk:
            tmp_file.write(chunk)
    tmp_file.close()
    return tmp_file.name

def pick_visual_query(keywords: str) -> str:
    """Query Pexels generica basata su keywords"""
    if not keywords or keywords.lower() == "none":
        return "person thinking emotion calm peaceful nature"
    
    # Pulisci keywords
    kw_clean = keywords.lower().replace(",", " ").strip()
    
    # Query generica + keywords
    return f"{kw_clean} person emotion lifestyle calm peaceful"

def fetch_clip_for_scene(scene_number: int, query: str, duration: float = 15.0):
    """Scarica clip da Pexels/Pixabay"""
    
    def try_pexels():
        if not PEXELS_API_KEY:
            return None
        headers = {"Authorization": PEXELS_API_KEY}
        params = {
            "query": query,
            "orientation": "portrait",  # 9:16 per shorts!
            "per_page": 20,
            "page": random.randint(1, 3),
        }
        resp = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            return None
        videos = resp.json().get("videos", [])
        if videos:
            video = random.choice(videos)
            for vf in video.get("video_files", []):
                # Cerca vertical video (portrait)
                if vf.get("width", 0) >= 720 and vf.get("height", 0) >= 1280:
                    return download_file(vf["link"])
        return None
    
    def try_pixabay():
        if not PIXABAY_API_KEY:
            return None
        params = {
            "key": PIXABAY_API_KEY,
            "q": query,
            "per_page": 20,
            "safesearch": "true",
            "min_width": 720,
        }
        resp = requests.get("https://pixabay.com/api/videos/", params=params, timeout=20)
        if resp.status_code != 200:
            return None
        hits = resp.json().get("hits", [])
        for hit in hits:
            videos = hit.get("videos", {})
            for quality in ["large", "medium", "small"]:
                if quality in videos and "url" in videos[quality]:
                    return download_file(videos[quality]["url"])
        return None
    
    # Priorità: Pexels → Pixabay
    for source_name, func in [("Pexels", try_pexels), ("Pixabay", try_pixabay)]:
        try:
            path = func()
            if path:
                print(f"🎥 Clip {scene_number}: '{query[:40]}...' → {source_name} ✓", flush=True)
                return path, duration
        except Exception as e:
            print(f"⚠️ {source_name}: {e}", flush=True)
    
    print(f"⚠️ NO CLIP per scena {scene_number}: '{query}'", flush=True)
    return None, None

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "jobs": len(jobs)})

@app.route("/status/<job_id>", methods=["GET"])
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    response = {
        "job_id": job_id,
        "status": job["status"],
        "created_at": job.get("created_at")
    }
    if job['status'] == 'completed':
        response['results'] = job.get('results', [])
    elif job['status'] == 'failed':
        response['error'] = job.get('error')
    
    return jsonify(response)

def process_social_shorts_async(job_id, data):
    """Genera 4 shorts (stesse clip, audio diverso per ogni social)"""
    job = jobs[job_id]
    job["status"] = "processing"
    
    video_clips_path = None
    scene_paths = []
    normalized_clips = []
    
    try:
        if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL]):
            raise RuntimeError("Config R2 mancante")
        
        channel_name = data.get("channel_name", "default")
        keywords = data.get("keywords", "")
        platforms = data.get("platforms", [])  # Array 4 oggetti {platform, audio_base64, description, hashtags}
        
        if len(platforms) != 4:
            raise RuntimeError(f"Servono 4 platforms, ricevuti: {len(platforms)}")
        
        print("=" * 80, flush=True)
        print(f"🎬 START AGENTE SOCIAL: {channel_name}, keywords: '{keywords}', {len(platforms)} platforms", flush=True)
        
        # 1. SCARICA 5 CLIP DA PEXELS/PIXABAY
        print(f"📥 Scarico {MAX_CLIPS} clip da Pexels/Pixabay...", flush=True)
        query = pick_visual_query(keywords)
        
        for i in range(MAX_CLIPS):
            clip_path, clip_dur = fetch_clip_for_scene(i + 1, query, 15.0)
            if clip_path and clip_dur:
                scene_paths.append((clip_path, clip_dur))
        
        print(f"✅ {len(scene_paths)}/{MAX_CLIPS} clip scaricate", flush=True)
        
        if len(scene_paths) < 3:
            raise RuntimeError(f"Troppe poche clip: {len(scene_paths)}/{MAX_CLIPS}")
        
        # 2. NORMALIZZA CLIP 9:16 (1080x1920)
        print("🔧 Normalizzo clip in 9:16...", flush=True)
        for i, (clip_path, _dur) in enumerate(scene_paths):
            try:
                normalized_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                normalized_path = normalized_tmp.name
                normalized_tmp.close()
                
                subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error", "-i", clip_path,
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an", normalized_path
                ], timeout=MAX_DURATION, check=True)
                
                if os.path.exists(normalized_path) and os.path.getsize(normalized_path) > 1000:
                    normalized_clips.append(normalized_path)
                    print(f"✅ Clip {i+1} normalizzata", flush=True)
            except Exception as e:
                print(f"⚠️ Errore normalize clip {i+1}: {e}", flush=True)
        
        if not normalized_clips:
            raise RuntimeError("Nessuna clip normalizzata")
        
        # 3. CONCATENA CLIP (video base senza audio)
        print("🔗 Concateno clip...", flush=True)
        concat_list_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
        for norm_path in normalized_clips:
            concat_list_tmp.write(f"file '{norm_path}'\n")
        concat_list_tmp.close()
        
        video_clips_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        video_clips_path = video_clips_tmp.name
        video_clips_tmp.close()
        
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_list_tmp.name,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an",
            video_clips_path
        ], timeout=MAX_DURATION, check=True)
        
        os.unlink(concat_list_tmp.name)
        print(f"✅ Video base concatenato: {video_clips_path}", flush=True)
        
        # 4. GENERA 4 SHORTS (loop per ogni social)
        print("🎬 Genero 4 shorts (audio diverso)...", flush=True)
        results = []
        s3_client = get_s3_client()
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        
        for idx, platform_data in enumerate(platforms):
            platform = platform_data.get("platform", f"Platform_{idx}")
            audio_base64 = platform_data.get("audio_base64")
            description = platform_data.get("description", "")
            hashtags = platform_data.get("hashtags", "")
            
            if not audio_base64:
                print(f"⚠️ Platform {platform}: audio_base64 mancante, skip", flush=True)
                continue
            
            print(f"🔧 [{idx+1}/4] Processing {platform}...", flush=True)
            
            # Decode audio
            try:
                audio_bytes = base64.b64decode(audio_base64)
            except Exception as e:
                print(f"❌ Decode audio failed per {platform}: {e}", flush=True)
                continue
            
            audio_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
            audio_tmp.write(audio_bytes)
            audio_tmp.close()
            
            # Convert audio a WAV
            audio_wav_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
            audio_wav_path = audio_wav_tmp.name
            audio_wav_tmp.close()
            
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error", "-i", audio_tmp.name,
                "-acodec", "pcm_s16le", "-ar", "48000", audio_wav_path
            ], timeout=120, check=True)
            
            os.unlink(audio_tmp.name)
            
            # Get audio duration
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", audio_wav_path
            ], stdout=subprocess.PIPE, text=True, timeout=10)
            audio_duration = float(probe.stdout.strip() or 60.0)
            
            # Merge video + audio
            final_video_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            final_video_path = final_video_tmp.name
            final_video_tmp.close()
            
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-stream_loop", "-1", "-i", video_clips_path,  # Loop video
                "-i", audio_wav_path,
                "-t", str(audio_duration),  # Taglia a durata audio
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
                final_video_path
            ], timeout=MAX_DURATION, check=True)
            
            os.unlink(audio_wav_path)
            
            # Upload R2
            platform_short = platform.replace(" ", "_")
            object_key = f"shorts/{channel_name}/{today}/{platform_short}_{uuid.uuid4().hex}.mp4"
            
            s3_client.upload_file(
                Filename=final_video_path,
                Bucket=R2_BUCKET_NAME,
                Key=object_key,
                ExtraArgs={"ContentType": "video/mp4"}
            )
            
            public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"
            
            results.append({
                "platform": platform,
                "video_url": public_url,
                "description": description,
                "hashtags": hashtags,
                "duration": round(audio_duration, 1)
            })
            
            os.unlink(final_video_path)
            print(f"✅ {platform}: {public_url}", flush=True)
        
        # Cleanup
        if video_clips_path and os.path.exists(video_clips_path):
            os.unlink(video_clips_path)
        for clip_path, _ in scene_paths:
            try:
                os.unlink(clip_path)
            except:
                pass
        for norm_path in normalized_clips:
            try:
                os.unlink(norm_path)
            except:
                pass
        
        print(f"🎉 {len(results)} SHORTS GENERATI!", flush=True)
        print("=" * 80, flush=True)
        
        job.update({
            "status": "completed",
            "results": results
        })
        
    except Exception as e:
        print(f"❌ ERRORE AGENTE SOCIAL: {e}", flush=True)
        job.update({"status": "failed", "error": str(e)})
    
    finally:
        Thread(target=lambda: cleanup_job_delayed(job_id), daemon=True).start()

def cleanup_job_delayed(job_id, delay=3600):
    import time
    time.sleep(delay)
    if job_id in jobs:
        del jobs[job_id]

@app.route("/generate", methods=["POST"])
def generate():
    try:
        data = request.get_json(force=True) or {}
        job_id = str(uuid.uuid4())
        
        jobs[job_id] = {
            "status": "queued",
            "created_at": dt.datetime.utcnow().isoformat(),
            "data": data
        }
        
        if len(jobs) > MAX_JOBS:
            old_jobs = sorted(jobs.keys(), key=lambda k: jobs[k]["created_at"])[:len(jobs)-MAX_JOBS]
            for oj in old_jobs:
                del jobs[oj]
        
        Thread(target=process_social_shorts_async, args=(job_id, data), daemon=True).start()
        
        print(f"🚀 Job {job_id} QUEUED: channel={data.get('channel_name')}, platforms={len(data.get('platforms', []))}", flush=True)
        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "queued",
            "message": "4 shorts generation started (check /status/<job_id>)"
        })
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
