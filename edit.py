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


# ---------- v15: ตัดต่อคลิปที่ถ่ายเองให้น่าดู ----------
# ย่อเป็นไฮไลต์ 15–30 วิ (เลือกช่วงที่ภาพเคลื่อนไหวดี กระจายตั้งแต่ต้นจนจบ) → ปรับสี
# → AI ดูภาพตัวอย่างแล้วเขียนชื่อเรื่อง/คำพากย์/แคปชันฮาๆ → พากย์ไทย + ซับ (เสียงเดิมเบาลง)
SOURCE_MAX = 180          # อ่านคลิปต้นฉบับได้ยาวสุดกี่วินาที (ไว้เลือกไฮไลต์)
HL_TRIGGER = 30           # ยาวกว่านี้ค่อยย่อ
HL_TARGET = 24            # ความยาวไฮไลต์
SEG = 3.0                 # ความยาวแต่ละช่วงที่ตัดมาต่อ
COLOR = 'eq=saturation=1.22:contrast=1.06:brightness=0.015:gamma=1.02,unsharp=5:5:0.4:5:5:0.0'
ORIG_VOL_WITH_VOICE = 0.22


def build_joined(items, workdir, max_total):
    parts, total = [], 0.0
    for i, it in enumerate(items):
        remain = max_total - total
        if remain < 0.5:
            break
        dst = os.path.join(workdir, f'part{i:02d}.mp4')
        if it['type'] == 'video':
            total += normalize_video(it['path'], dst, remain)
        else:
            total += normalize_image(it['path'], dst, min(IMAGE_SECONDS, remain))
        parts.append(dst)
    if not parts:
        raise RuntimeError('ไม่มีไฟล์ให้ตัดต่อ')
    listfile = os.path.join(workdir, 'list.txt')
    with open(listfile, 'w') as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    joined = os.path.join(workdir, 'joined.mp4')
    run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', listfile, '-c', 'copy', joined])
    return joined


def motion_scores(path):
    """ค่าการเปลี่ยนแปลงของภาพ ~5 ครั้งต่อวินาที (ภาพนิ่ง = ต่ำ, กล้องสะบัดแรง = สูงมาก)"""
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf',
                        'fps=5,scale=160:-2,tblend=all_mode=difference,signalstats,'
                        'metadata=print:key=lavfi.signalstats.YAVG:file=-',
                        '-an', '-f', 'null', '-'], capture_output=True, text=True)
    vals = []
    for line in p.stdout.splitlines():
        if 'YAVG=' in line:
            try:
                vals.append(float(line.split('YAVG=')[1]))
            except ValueError:
                pass
    return vals  # index i ≈ เวลา i/5 วินาที


def pick_segments(scores, duration, target=HL_TARGET, seg=SEG):
    """แบ่งคลิปเป็นช่วงๆ เท่ากัน แต่ละช่วงเลือกหน้าต่าง seg วินาทีที่ดีที่สุด → เรื่องยังเดินจากต้นถึงจบ
    คะแนน: ภาพที่ขยับพอดีดีที่สุด — นิ่งเกินหรือสะบัดแรงเกิน (บนสุด 10%) ถูกหักคะแนน"""
    n = max(1, int(round(target / seg)))
    if not scores or duration <= target + 0.5:
        return [(0.0, duration)]
    srt = sorted(scores)
    p90 = srt[int(len(srt) * 0.9) - 1] if len(srt) > 10 else srt[-1]
    adj = [v if v <= p90 else max(0.0, p90 - (v - p90)) for v in scores]
    rate = len(scores) / max(duration, 0.1)
    win = max(1, int(seg * rate))
    bucket = duration / n
    out = []
    for b in range(n):
        lo, hi = b * bucket, min(duration, (b + 1) * bucket)
        best, best_t = -1.0, lo
        t = lo
        while t + seg <= hi + 1e-6:
            i0 = int(t * rate)
            chunk = adj[i0:i0 + win]
            sc = sum(chunk) / len(chunk) if chunk else 0.0
            if sc > best:
                best, best_t = sc, t
            t += 0.5
        if hi - lo < seg:  # ช่วงสั้นกว่าหน้าต่าง → เอาทั้งช่วง
            best_t = lo
        out.append((round(best_t, 2), round(min(best_t + seg, duration), 2)))
    # ตัดช่วงซ้อนทับ
    merged = []
    for a, b in out:
        if merged and a < merged[-1][1]:
            a = merged[-1][1]
        if b - a >= 1.0:
            merged.append((a, b))
    return merged or [(0.0, min(duration, target))]


def cut_segments(src, segs, dst):
    if len(segs) == 1 and segs[0][0] == 0:
        run(['ffmpeg', '-y', '-v', 'error', '-i', src, '-t', f'{segs[0][1]:.3f}', '-c', 'copy', dst])
        return
    fc, labels = [], ''
    for i, (a, b) in enumerate(segs):
        fc.append(f'[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]')
        fc.append(f'[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,afade=t=in:d=0.08,'
                  f'afade=t=out:st={max(0, b - a - 0.08):.2f}:d=0.08[a{i}]')
        labels += f'[v{i}][a{i}]'
    fc.append(f'{labels}concat=n={len(segs)}:v=1:a=1[v][a]')
    run(['ffmpeg', '-y', '-v', 'error', '-i', src, '-filter_complex', ';'.join(fc),
         '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
         '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2', dst])


def sample_frames(src, duration, n=4):
    """ภาพตัวอย่างเล็กๆ ส่งให้ AI ดู (base64 jpeg)"""
    import base64
    out = []
    for k in range(n):
        t = duration * (k + 0.5) / n
        p = f'{src}.f{k}.jpg'
        try:
            run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{t:.2f}', '-i', src, '-frames:v', '1',
                 '-vf', 'scale=432:768', '-q:v', '6', p])
            with open(p, 'rb') as f:
                out.append(base64.b64encode(f.read()).decode())
        except Exception as e:
            log('  ดึงภาพตัวอย่างไม่ได้:', str(e)[-120:])
    return out


def narration_track(lines, duration, workdir):
    """สร้างเสียงพากย์ทีละประโยค วางต่อกัน คืน (ไฟล์เสียงรวม, [(เริ่ม, จบ, ข้อความ)])"""
    clips = []
    for i, text in enumerate(lines):
        aud = os.path.join(workdir, f'nar{i:02d}.mp3')
        d = tts(text, aud)
        if d:
            clips.append((aud, d, text))
    # ตัดประโยคท้ายๆ ทิ้งถ้ารวมแล้วยาวเกินคลิป
    while clips and 0.6 + sum(c[1] for c in clips) + 0.35 * (len(clips) - 1) > duration - 0.3:
        log(f'  ตัดคำพากย์ที่เกินความยาวคลิป: {clips[-1][2]}')
        clips.pop()
    # กระจายประโยคให้ทั่วคลิป (ประโยคสุดท้ายจบใกล้ท้ายคลิป) แทนที่จะพูดรวดเดียวช่วงต้น
    t, cues, inputs = 0.6, [], []
    slot = (duration - 0.6) / max(1, len(clips))
    for i, (aud, d, text) in enumerate(clips):
        remaining = sum(c[1] + 0.35 for c in clips[i:])
        start = max(t, min(0.6 + i * slot, duration - 0.3 - remaining + 0.35))
        cues.append((start, start + d, text))
        inputs.append((aud, start))
        t = start + d + 0.35
    if not inputs:
        return None, []
    cmd = ['ffmpeg', '-y', '-v', 'error']
    for aud, _ in inputs:
        cmd += ['-i', aud]
    fc = ';'.join(f'[{i}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={int(st*1000)}:all=1[n{i}]'
                  for i, (_, st) in enumerate(inputs))
    fc += ';' + ''.join(f'[n{i}]' for i in range(len(inputs))) + \
        f'amix=inputs={len(inputs)}:normalize=0,apad,atrim=0:{duration:.3f}[out]'
    out = os.path.join(workdir, 'narration.m4a')
    run(cmd + ['-filter_complex', fc, '-map', '[out]', '-c:a', 'aac', '-b:a', '128k', out])
    return out, cues


def encode_capped(inputs, filter_complex, maps, out_path, duration):
    """เข้ารหัส h264 ให้ไฟล์ไม่เกิน 24MB (crf 23 + เพดานบิตเรต → ถ้ายังเกินลดบิตเรต)"""
    dur = max(1.0, min(duration, MAX_SECONDS))
    cap = int(MAX_BYTES * 8 * 0.85 / dur / 1000) - 128
    attempts = [['-crf', '23', '-maxrate', f'{cap}k', '-bufsize', f'{cap*2}k']]
    for margin in (0.70, 0.55):
        kbps = int(MAX_BYTES * 8 * margin / dur / 1000) - 128
        attempts.append(['-b:v', f'{kbps}k', '-maxrate', f'{int(kbps*1.2)}k', '-bufsize', f'{kbps*2}k'])
    for rate in attempts:
        run(['ffmpeg', '-y', '-v', 'error', *inputs, '-filter_complex', filter_complex, *maps,
             '-c:v', 'libx264', '-preset', 'veryfast', *rate, '-pix_fmt', 'yuv420p', '-r', str(FPS),
             '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', '-t', f'{dur:.3f}', str(out_path)])
        size = os.path.getsize(out_path)
        log(f'ไฟล์ผลลัพธ์ {size/1048576:.1f}MB ({" ".join(rate[:2])})')
        if size <= MAX_BYTES:
            return
    raise RuntimeError('ไฟล์ใหญ่เกิน 24MB แม้บีบอัดแล้ว')


def no_emoji(text):
    """ฟอนต์ไทยบนคลิปไม่มีอีโมจิ (จะขึ้นเป็นกล่องสี่เหลี่ยม) → ตัดทิ้ง"""
    import re
    t = re.sub('[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]', '', str(text or ''))
    return re.sub(r'\s+', ' ', t).strip()


def enhance_edit(items, title, out_path, workdir, script_fn=None):
    """script_fn(duration, frames) -> {'title','caption','lines'} หรือ {} (ไม่มี AI)"""
    joined = build_joined(items, workdir, SOURCE_MAX)
    jdur = probe(joined)['duration']
    stats = {'source_sec': round(jdur, 1)}
    clip = joined
    if jdur > HL_TRIGGER:
        segs = pick_segments(motion_scores(joined), jdur)
        clip = os.path.join(workdir, 'highlight.mp4')
        cut_segments(joined, segs, clip)
        stats['segments'] = len(segs)
        log(f'  ไฮไลต์ {len(segs)} ช่วง: {segs}')
    elif jdur > MAX_SECONDS:
        clip = os.path.join(workdir, 'trim.mp4')
        cut_segments(joined, [(0, MAX_SECONDS)], clip)
    cdur = min(probe(clip)['duration'], MAX_SECONDS)
    stats['out_sec'] = round(cdur, 1)

    script = {}
    if script_fn:
        try:
            script = script_fn(cdur, sample_frames(clip, cdur)) or {}
        except Exception as e:
            log('  ขอบทจาก AI ไม่สำเร็จ:', str(e)[-200:])
    stats['script'] = script.get('source', 'none')
    title = no_emoji(title or script.get('title') or '')
    narr, cues = narration_track([no_emoji(x) for x in (script.get('lines') or [])], cdur, workdir)
    stats['voice_lines'] = len(cues)

    font = font_path()
    vchain = f'[0:v]{COLOR}'
    if font and title:
        tf = os.path.join(workdir, 'title.txt')
        with open(tf, 'w', encoding='utf-8') as f:
            f.write('\n'.join(wrap_thai(title[:40], width=18)[:2]))
        vchain += (f",drawtext=fontfile='{font}':textfile='{tf}':fontsize=76:fontcolor=white:line_spacing=14:"
                   f"box=1:boxcolor=black@0.55:boxborderw=28:x=(w-text_w)/2:y=h*0.11:"
                   f"enable='lt(t,{TITLE_SECONDS})'")
    if font:
        for i, (a, b, text) in enumerate(cues):
            sf = os.path.join(workdir, f'cue{i:02d}.txt')
            with open(sf, 'w', encoding='utf-8') as f:
                f.write('\n'.join(wrap_thai(text, width=18)[:3]))
            vchain += (f",drawtext=fontfile='{font}':textfile='{sf}':fontsize=62:fontcolor=white:line_spacing=16:"
                       f"borderw=5:bordercolor=black@0.9:box=1:boxcolor=black@0.35:boxborderw=18:"
                       f"x=(w-text_w)/2:y=h*0.70:enable='between(t,{a:.2f},{b:.2f})'")
    elif title or cues:
        log('⚠️ ไม่พบฟอนต์ไทย — ข้ามตัวหนังสือบนคลิป')
    vchain += ',format=yuv420p,setsar=1[v]'
    inputs = ['-i', clip]
    if narr:
        inputs += ['-i', narr]
        achain = (f'[0:a]volume={ORIG_VOL_WITH_VOICE}[o];[1:a]volume=1.6[n];'
                  f'[o][n]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[a]')
    else:
        achain = '[0:a]anull[a]'
    encode_capped(inputs, vchain + ';' + achain, ['-map', '[v]', '-map', '[a]'], out_path, cdur)

    thumb = os.path.join(workdir, 'thumb.jpg')
    run(['ffmpeg', '-y', '-v', 'error', '-ss', '0.8', '-i', str(out_path), '-frames:v', '1',
         '-vf', 'scale=540:960', '-q:v', '4', thumb])
    return thumb, probe(out_path)['duration'], stats


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
            def script_fn(duration, frames):
                body = json.dumps({'duration': round(duration, 1), 'frames': frames}).encode()
                return api('POST', f'/edit/script?cid={cid}', token, body,
                           {'Content-Type': 'application/json'}, timeout=120)
            thumb, dur, stats = enhance_edit(items, job.get('title') or '', out, wd, script_fn)
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
    if len(sys.argv) > 1 and sys.argv[1] == '--enhance':
        # python edit.py --enhance script.json|- "ชื่อเรื่อง" out.mp4 file1 ...  (ทดสอบตัดต่อแบบใหม่ ไม่ต่อ Worker)
        sj, ttl, outp, files = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]
        fixed = {} if sj == '-' else json.load(open(sj, encoding='utf-8'))
        its = [{'type': 'image' if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) else 'video', 'path': f}
               for f in files]
        seen = {}
        def fake(duration, frames):
            seen.update(duration=duration, frames=len(frames))
            return fixed
        with tempfile.TemporaryDirectory() as wd:
            th, d, st = enhance_edit(its, ttl, outp, wd, fake)
            os.replace(th, os.path.splitext(outp)[0] + '.jpg')
        log(f'เสร็จ: {outp} ยาว {d:.1f}s {st} ai_saw={seen}')
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == '--local':
        sys.exit(main_local(sys.argv[2:]))
    sys.exit(main_remote())
