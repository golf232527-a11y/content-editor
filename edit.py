#!/usr/bin/env python3
"""ตัดต่อคลิปอัตโนมัติสำหรับบอทคอนเทนต์ (รันบน GitHub Actions)

ขั้นตอน: ขอรายการงานจาก Worker → ดาวน์โหลดไฟล์ → ตัดต่อด้วย ffmpeg → อัปโหลดผลกลับ
- แนวตั้ง 1080x1920 (ภาพเดิมอยู่ตรงกลาง พื้นหลังเบลอ ไม่ครอปเนื้อหาทิ้ง)
- คลิปหลายไฟล์ต่อกันตามลำดับ, รูปแต่ละรูปค้าง 3 วินาที (ซูมช้าๆ)
- ความยาวรวมไม่เกิน 60 วินาที
- ข้อความหัวเรื่อง (ชื่องาน) 3 วินาทีแรก
- ไฟล์ผลลัพธ์ไม่เกิน 24MB (ถ้าเกินจะบีบอัดซ้ำ)

ยืนยันตัวตนกับ Worker ด้วย OIDC token ของ GitHub Actions — ไม่มีรหัสลับเก็บใน repo
หรือใช้ --local เพื่อทดสอบกับไฟล์ในเครื่องโดยไม่ต่อ Worker
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

W, H, FPS = 1080, 1920, 30
MAX_SECONDS = 60
IMAGE_SECONDS = 3.0
TITLE_SECONDS = 3.0
MAX_BYTES = 24 * 1024 * 1024
FONT_CANDIDATES = [
    '/usr/share/fonts/opentype/tlwg/Loma-Bold.otf',
    '/usr/share/fonts/truetype/tlwg/Loma-Bold.ttf',
    '/usr/share/fonts/opentype/tlwg/Garuda-Bold.otf',
    '/usr/share/fonts/truetype/tlwg/Garuda-Bold.ttf',
]
UA = 'content-editor/1.0'


def log(*a):
    print(*a, flush=True)


def run(cmd):
    log('$', ' '.join(str(c) for c in cmd)[:400])
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f'command failed ({p.returncode}): {p.stderr[-1500:]}')
    return p.stdout


def probe(path):
    out = run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', str(path)])
    info = json.loads(out)
    v = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), None)
    a = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), None)
    dur = float(info.get('format', {}).get('duration') or (v or {}).get('duration') or 0)
    return {'has_video': v is not None, 'has_audio': a is not None, 'duration': dur}


def font_path():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    try:
        out = subprocess.run(['fc-match', '-f', '%{file}', 'Loma:bold'], capture_output=True, text=True).stdout
        if out and os.path.exists(out):
            return out
    except Exception:
        pass
    return None


# ---------- แปลงไฟล์แต่ละชิ้นเป็นคลิปมาตรฐาน 1080x1920 30fps มีเสียง ----------
BG = (f'scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},'
      f'boxblur=30:3,eq=brightness=-0.08')
FG = f'scale={W}:{H}:force_original_aspect_ratio=decrease'


def normalize_video(src, dst, max_len):
    info = probe(src)
    if not info['has_video']:
        raise RuntimeError('ไฟล์ไม่มีภาพวิดีโอ')
    length = min(info['duration'] or max_len, max_len)
    vf = (f'[0:v]split=2[a][b];[a]{BG}[bg];[b]{FG}[fg];'
          f'[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},format=yuv420p,setsar=1[v]')
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', str(src)]
    if info['has_audio']:
        amap = ['-map', '0:a:0']
    else:
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo']
        amap = ['-map', '1:a:0']
    cmd += ['-filter_complex', vf, '-map', '[v]', *amap, '-t', f'{length:.3f}',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2', str(dst)]
    run(cmd)
    return length


def normalize_image(src, dst, seconds):
    frames = int(seconds * FPS)
    # ซูมช้าๆ จาก 100% → ~106% ให้ภาพนิ่งดูมีชีวิต
    zoom = f"zoompan=z='1+0.06*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS}"
    vf = (f'[0:v]scale={W*2}:{H*2}:force_original_aspect_ratio=decrease,split=2[a][b];'
          f'[a]{BG}[bg];[b]{FG}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,'
          f'scale={W*2}:{H*2},{zoom},format=yuv420p,setsar=1[v]')
    run(['ffmpeg', '-y', '-v', 'error', '-loop', '1', '-i', str(src),
         '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
         '-filter_complex', vf, '-map', '[v]', '-map', '1:a:0', '-t', f'{seconds:.3f}',
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
         '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2', str(dst)])
    return seconds


def make_video(items, title, out_path, workdir, already_vertical=False):
    """items: [{'type': 'video'|'image', 'path': ...}] ตามลำดับ"""
    if not items:
        raise RuntimeError('ไม่มีไฟล์ให้ตัดต่อ')
    parts, total = [], 0.0
    if already_vertical:
        parts = [it['path'] for it in items]
    for i, it in enumerate([] if already_vertical else items):
        remain = MAX_SECONDS - total
        if remain < 0.5:
            break
        dst = os.path.join(workdir, f'part{i:02d}.mp4')
        if it['type'] == 'video':
            total += normalize_video(it['path'], dst, remain)
        else:
            total += normalize_image(it['path'], dst, min(IMAGE_SECONDS, remain))
        parts.append(dst)

    listfile = os.path.join(workdir, 'list.txt')
    with open(listfile, 'w') as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    joined = os.path.join(workdir, 'joined.mp4')
    run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', listfile,
         '-c', 'copy', str(joined)])

    # ข้อความหัวเรื่อง 3 วินาทีแรก
    font = font_path()
    vf = 'null'
    if title and font:
        tf = os.path.join(workdir, 'title.txt')
        with open(tf, 'w', encoding='utf-8') as f:
            f.write(title.strip()[:40])
        vf = (f"drawtext=fontfile='{font}':textfile='{tf}':fontsize=72:fontcolor=white:"
              f"box=1:boxcolor=black@0.55:boxborderw=28:x=(w-text_w)/2:y=h*0.12:"
              f"enable='lt(t,{TITLE_SECONDS})'")
    elif title:
        log('⚠️ ไม่พบฟอนต์ไทย — ข้ามข้อความหัวเรื่อง')

    # รอบแรกใช้คุณภาพคงที่ (crf 23) ถ้าใหญ่เกิน → คุมบิตเรตให้พอดี 24MB
    # (คลิปถ่ายจริงที่ภาพละเอียด/กล้องสั่น เช่น เดินป่า crf 32 ยังเกิน 24MB)
    jdur = min(probe(joined)['duration'] or MAX_SECONDS, MAX_SECONDS)
    common_in = ['ffmpeg', '-y', '-v', 'error', '-i', joined, '-vf', vf]
    common_out = ['-pix_fmt', 'yuv420p', '-r', str(FPS), '-c:a', 'aac', '-b:a', '128k',
                  '-movflags', '+faststart', '-t', str(MAX_SECONDS), str(out_path)]
    cap = int(MAX_BYTES * 8 * 0.85 / jdur / 1000) - 128
    # รอบแรก: crf 23 แต่มีเพดานบิตเรต — คลิปง่ายได้คุณภาพดี คลิปยากไม่เกินขนาด ในรอบเดียว
    attempts = [['-crf', '23', '-maxrate', f'{cap}k', '-bufsize', f'{cap*2}k']]
    for margin in (0.70, 0.55):
        kbps = int(MAX_BYTES * 8 * margin / jdur / 1000) - 128
        attempts.append(['-b:v', f'{kbps}k', '-maxrate', f'{int(kbps*1.2)}k', '-bufsize', f'{kbps*2}k'])
    for rate in attempts:
        run(common_in + ['-c:v', 'libx264', '-preset', 'veryfast'] + rate + common_out)
        size = os.path.getsize(out_path)
        log(f'ไฟล์ผลลัพธ์ {size/1048576:.1f}MB ({" ".join(rate[:2])})')
        if size <= MAX_BYTES:
            break
    else:
        raise RuntimeError('ไฟล์ใหญ่เกิน 24MB แม้บีบอัดแล้ว')

    thumb = os.path.join(workdir, 'thumb.jpg')
    run(['ffmpeg', '-y', '-v', 'error', '-ss', '0.5', '-i', str(out_path), '-frames:v', '1',
         '-vf', 'scale=540:960', '-q:v', '4', thumb])
    return thumb, probe(out_path)['duration']


# ---------- สร้างวิดีโอจากโจทย์ (AI) ----------
VOICE = os.environ.get('TTS_VOICE', 'th-TH-PremwadeeNeural')
IMG_STYLE = ', cinematic, vertical composition, high detail, natural light, no text, no watermark'
THAI_CHARS_PER_SEC = 11.0


_last_image_at = [0.0]
IMAGE_GAP_SECONDS = 16  # Pollinations แบบไม่ใช้คีย์ ยอมให้ขอรูปทีละรูป เว้นช่วง ~15 วินาที


def fetch_ai_image(prompt, dst, seed):
    import time
    import urllib.parse
    url = ('https://image.pollinations.ai/prompt/' + urllib.parse.quote(prompt + IMG_STYLE)
           + f'?width={W}&height={H}&nologo=true&seed={seed}')
    last = None
    for attempt in range(4):
        wait = IMAGE_GAP_SECONDS - (time.time() - _last_image_at[0])
        if wait > 0:
            time.sleep(wait)
        try:
            download(url, dst)
            _last_image_at[0] = time.time()
            if os.path.getsize(dst) > 10_000 and probe(dst)['has_video']:
                return True
            last = 'ไฟล์รูปเสีย'
        except Exception as e:
            _last_image_at[0] = time.time()
            last = e
            if '402' in str(e) or '429' in str(e):
                time.sleep(15 * (attempt + 1))  # โดนจำกัดความถี่ → รอนานขึ้นแล้วลองใหม่
        log(f'  รูปฉากล้มเหลว (ครั้งที่ {attempt+1}): {last}')
    return False


def fallback_image(dst, idx):
    colors = ['0x1e3a5f', '0x3b1e5f', '0x1e5f3b', '0x5f3b1e', '0x5f1e3b', '0x1e5f5f']
    c = colors[idx % len(colors)]
    run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
         f'gradients=s={W}x{H}:c0={c}:c1=0x000000:x0=0:y0=0:x1={W}:y1={H}:d=1', '-frames:v', '1', dst])


def tts(text, dst):
    """เสียงพากย์ไทย: edge-tts (เสียงธรรมชาติกว่า) → ถ้าไม่ได้ใช้ Google TTS (gTTS)"""
    attempts = [
        ['edge-tts', '--voice', VOICE, '--text', text, '--write-media', dst],
        ['edge-tts', '--voice', 'th-TH-NiwatNeural', '--text', text, '--write-media', dst],
        ['gtts-cli', '--lang', 'th', '--output', dst, text],
    ]
    for cmd in attempts:
        try:
            if os.path.exists(dst):
                os.remove(dst)
            run(cmd)
            if os.path.exists(dst) and os.path.getsize(dst) > 1000:
                return probe(dst)['duration']
        except Exception as e:
            log(f'  เสียงพากย์ ({cmd[0]}) ล้มเหลว: {str(e)[-200:]}')
    return None


def wrap_thai(text, width=16):
    """ตัดบรรทัดซับไทย: เว้นวรรคก่อน ถ้าคำยาวเกินค่อยตัดตามตัวอักษร (ไม่แยกสระ/วรรณยุกต์ออกจากพยัญชนะ)"""
    import unicodedata

    def clusters(word):
        out = []
        for ch in word:
            if out and unicodedata.category(ch) == 'Mn':
                out[-1] += ch
            else:
                out.append(ch)
        return out

    lines, cur = [], []
    for word in text.split():
        cl = clusters(word)
        if cur and len(cur) + 1 + len(cl) > width:
            lines.append(''.join(cur)); cur = []
        if cur:
            cur.append(' ')
        cur.extend(cl)
        while len(cur) > width:
            lines.append(''.join(cur[:width])); cur = cur[width:]
    if cur:
        lines.append(''.join(cur))
    return lines[:4]


def scene_clip(img, audio, audio_dur, text, dst, workdir, idx):
    dur = max(2.5, (audio_dur or len(text) / THAI_CHARS_PER_SEC) + 0.4)
    frames = int(dur * FPS)
    zoom = (f"zoompan=z='1+0.08*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={W}x{H}:fps={FPS}")
    vf = (f'[0:v]scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},{zoom}')
    font = font_path()
    if font and text:
        sub = os.path.join(workdir, f'sub{idx:02d}.txt')
        with open(sub, 'w', encoding='utf-8') as f:
            f.write('\n'.join(wrap_thai(text)))
        vf += (f",drawtext=fontfile='{font}':textfile='{sub}':fontsize=60:fontcolor=white:"
               f"line_spacing=18:box=1:boxcolor=black@0.5:boxborderw=22:x=(w-text_w)/2:y=h*0.70")
    vf += ',format=yuv420p,setsar=1[v]'
    cmd = ['ffmpeg', '-y', '-v', 'error', '-loop', '1', '-i', img]
    if audio:
        cmd += ['-i', audio]
    else:
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo']
    cmd += ['-filter_complex', vf + ';[1:a]apad,aresample=44100,aformat=channel_layouts=stereo[a]',
            '-map', '[v]', '-map', '[a]', '-t', f'{dur:.3f}',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2', dst]
    run(cmd)
    return dur


def generate_video(script, out_path, workdir):
    scenes = (script or {}).get('scenes') or []
    if not scenes:
        raise RuntimeError('ไม่มีบทวิดีโอ')
    parts, total, ai_ok, voice_ok = [], 0.0, 0, 0
    for i, sc in enumerate(scenes[:10]):
        text = (sc.get('text') or '').strip()
        if total >= MAX_SECONDS - 2:
            break
        img = os.path.join(workdir, f'img{i:02d}.jpg')
        got_ai = fetch_ai_image(sc.get('image_prompt') or text, img, seed=1000 + i)
        if got_ai:
            ai_ok += 1
        else:
            fallback_image(img, i)
        aud = os.path.join(workdir, f'voice{i:02d}.mp3')
        adur = tts(text, aud) if text else None
        if adur:
            voice_ok += 1
        dst = os.path.join(workdir, f'scene{i:02d}.mp4')
        total += scene_clip(img, aud if adur else None, adur, text, dst, workdir, i)
        parts.append(dst)
        log(f'  ฉาก {i+1}: รูป{"AI" if got_ai else "สำรอง"} เสียง{"มี" if adur else "ไม่มี"} รวม {total:.1f}s')
    items = [{'type': 'video', 'path': p} for p in parts]
    thumb, dur = make_video(items, script.get('title') or '', out_path, workdir, already_vertical=True)
    return thumb, dur, {'scenes': len(parts), 'ai_images': ai_ok, 'voice': voice_ok}


# ---------- ต่อกับ Worker ----------
def oidc_token():
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=content-bot'
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN'], 'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)['value']


def api(method, path, token, data=None, headers=None, timeout=120):
    # ขอ OIDC token ใหม่ทุกครั้ง (token อายุสั้น งานตัดต่อนานอาจเกินอายุ)
    if os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'):
        token = oidc_token()
    base = os.environ['WORKER_URL'].rstrip('/')
    h = {'Authorization': 'Bearer ' + token, 'User-Agent': UA}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else {}


def download(url, dst):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=180) as r, open(dst, 'wb') as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def multipart(fields, files):
    boundary = '----contenteditor' + os.urandom(8).hex()
    out = bytearray()
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (fname, ctype, path) in files.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                f'Content-Type: {ctype}\r\n\r\n').encode()
        with open(path, 'rb') as f:
            out += f.read()
        out += b'\r\n'
    out += f'--{boundary}--\r\n'.encode()
    return bytes(out), f'multipart/form-data; boundary={boundary}'


def process_job(job, token):
    cid = job['id']
    log(f"=== C{cid} {job.get('title')} mode={job.get('mode')} ({len(job.get('media') or [])} ไฟล์)")
    with tempfile.TemporaryDirectory() as wd:
        out = os.path.join(wd, 'out.mp4')
        stats = {}
        if job.get('mode') == 'generate':
            thumb, dur, stats = generate_video(job.get('script'), out, wd)
        else:
            items = []
            for i, m in enumerate(job['media']):
                ext = 'mp4' if m['type'] == 'video' else 'jpg'
                p = os.path.join(wd, f'src{i:02d}.{ext}')
                download(m['url'], p)
                items.append({'type': m['type'], 'path': p})
            thumb, dur = make_video(items, job.get('title') or '', out, wd)
        body, ctype = multipart({'duration': f'{dur:.1f}', 'stats': json.dumps(stats)},
                                {'video': ('edited.mp4', 'video/mp4', out),
                                 'thumb': ('thumb.jpg', 'image/jpeg', thumb)})
        res = api('POST', f'/edit/result?cid={cid}', token, body, {'Content-Type': ctype}, timeout=300)
        log('อัปโหลดแล้ว:', res)


def main_remote():
    token = oidc_token()
    jobs = api('GET', '/edit/jobs', token).get('jobs', [])
    log(f'งานรอตัดต่อ {len(jobs)} งาน')
    failed = 0
    for job in jobs:
        try:
            process_job(job, token)
        except Exception as e:
            failed += 1
            log(f'❌ C{job.get("id")} ล้มเหลว: {e}')
            try:
                api('POST', f"/edit/fail?cid={job['id']}", token,
                    json.dumps({'error': str(e)[:300]}).encode(), {'Content-Type': 'application/json'})
            except Exception as e2:
                log('แจ้งความล้มเหลวไม่สำเร็จ:', e2)
    return 1 if failed else 0


def main_local(args):
    # python edit.py --local "ชื่อเรื่อง" out.mp4 file1 file2 ...
    title, out, files = args[0], args[1], args[2:]
    items = [{'type': 'image' if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) else 'video', 'path': f}
             for f in files]
    with tempfile.TemporaryDirectory() as wd:
        thumb, dur = make_video(items, title, out, wd)
        os.replace(thumb, os.path.splitext(out)[0] + '.jpg')
    log(f'เสร็จ: {out} ยาว {dur:.1f} วินาที')
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--gen':
        # python edit.py --gen script.json out.mp4  (ทดสอบสร้างจากบท)
        with open(sys.argv[2], encoding='utf-8') as f:
            sc = json.load(f)
        with tempfile.TemporaryDirectory() as wd:
            th, d, st = generate_video(sc, sys.argv[3], wd)
            os.replace(th, os.path.splitext(sys.argv[3])[0] + '.jpg')
        log(f'เสร็จ: {sys.argv[3]} ยาว {d:.1f}s {st}')
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == '--local':
        sys.exit(main_local(sys.argv[2:]))
    sys.exit(main_remote())
