#!/usr/bin/env python3
"""Conversor automático de videos para mojcaestudio.com.

1. Pide al sitio la lista de videos sin versión liviana.
2. Baja cada original y arma con ffmpeg:
     - versión 1080 (el lado corto a 1080 como máximo), H.264, lista para web
     - portada JPG
     - vista previa de 8 segundos, muda y chica (para el mosaico y los álbumes)
3. Sube todo de vuelta en pedazos y avisa que terminó.
"""
import json, math, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request, uuid

SITE = os.environ.get('SITE', 'https://mojcaestudio.com').rstrip('/')
API = SITE + '/api/convert.php'
UA = 'MOJCA-Conversor/1.0 (+github-actions)'
START = time.time()
TIME_BUDGET = 320 * 60  # deja margen antes del límite del trabajo

_tok = {'v': None, 't': 0}
def token():
    if _tok['v'] and time.time() - _tok['t'] < 120:
        return _tok['v']
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=mojcaestudio'
    req = urllib.request.Request(url, headers={'Authorization': 'bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urllib.request.urlopen(req, timeout=30) as r:
        _tok['v'] = json.load(r)['value']; _tok['t'] = time.time()
    return _tok['v']

def call(method, params=None, fields=None, file_bytes=None, retries=4):
    for attempt in range(retries):
        try:
            headers = {'User-Agent': UA, 'Authorization': 'Bearer ' + token()}
            url = API
            data = None
            if method == 'GET':
                url += '?' + urllib.parse.urlencode(params or {})
            else:
                b = uuid.uuid4().hex
                parts = []
                for k, v in (fields or {}).items():
                    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
                if file_bytes is not None:
                    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="chunk"; filename="chunk.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + file_bytes + b'\r\n')
                parts.append(f'--{b}--\r\n'.encode())
                data = b''.join(parts)
                headers['Content-Type'] = 'multipart/form-data; boundary=' + b
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:500]
            if e.code == 409:
                try: return json.loads(body)
                except Exception: pass
            if e.code in (401, 403, 404, 400) and attempt == retries - 1:
                raise RuntimeError(f'HTTP {e.code}: {body}')
            print(f'  ! HTTP {e.code}: {body}', flush=True)
        except Exception as e:
            print(f'  ! {e}', flush=True)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError('el sitio no respondió')

def upload(vid, kind, path, chunk):
    size = os.path.getsize(path)
    total = max(1, math.ceil(size / chunk))
    with open(path, 'rb') as f:
        i = 0
        while i < total:
            f.seek(i * chunk)
            r = call('POST', fields={'action': 'chunk', 'id': vid, 'kind': kind, 'index': i, 'total': total, 'offset': i * chunk}, file_bytes=f.read(chunk))
            if r.get('error') == 'offset':  # se perdió un pedazo: vuelve a empezar desde donde quedó
                have = int(r.get('have', 0))
                i = have // chunk if have % chunk == 0 else 0
                continue
            if not r.get('success'):
                raise RuntimeError(r.get('error', 'error subiendo'))
            i += 1
    return r.get('url')

def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height:stream_side_data=rotation:format=duration', '-of', 'json', path], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = j['streams'][0]
    w, h = int(s['width']), int(s['height'])
    rot = 0
    for sd in s.get('side_data_list', []) or []:
        if 'rotation' in sd: rot = abs(int(sd['rotation']))
    if rot in (90, 270): w, h = h, w
    has_audio = bool(json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index', '-of', 'json', path], capture_output=True, text=True).stdout or '{}').get('streams'))
    return w, h, float(j['format'].get('duration') or 0), has_audio

def ff(args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + args, check=True)

def scale_short(target):
    # lado corto = target como máximo, sin agrandar; medidas pares
    return f"scale='if(lt(iw,ih),min({target},iw),-2)':'if(lt(iw,ih),-2,min({target},ih))',setsar=1"

def convert(item, chunk):
    vid = item['id']
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'original' + os.path.splitext(item['src'])[1])
        print(f"→ {item['src']} ({item['size'] / 1048576:.0f} MB)", flush=True)
        req = urllib.request.Request(item['url'], headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=600) as r, open(src, 'wb') as f:
            while True:
                b = r.read(1 << 20)
                if not b: break
                f.write(b)
        w, h, dur, has_audio = probe(src)
        print(f'  {w}x{h}, {dur:.1f} s', flush=True)
        hd, poster, prev = (os.path.join(d, n) for n in ('hd.mp4', 'portada.jpg', 'prev.mp4'))
        vf = scale_short(1080) + ',format=yuv420p'
        audio = ['-c:a', 'aac', '-b:a', '160k', '-ac', '2'] if has_audio else ['-an']
        ff(['-i', src, '-map', '0:v:0'] + (['-map', '0:a:0'] if has_audio else []) + ['-vf', vf, '-c:v', 'libx264', '-preset', 'medium', '-crf', '21', '-maxrate', '9M', '-bufsize', '18M', '-profile:v', 'high'] + audio + ['-movflags', '+faststart', hd])
        t0 = min(1.2, dur * 0.12)
        ff(['-ss', f'{t0:.2f}', '-i', src, '-frames:v', '1', '-vf', scale_short(720), '-q:v', '3', poster])
        plen = max(1.0, min(8.0, dur - t0 - 0.1))
        ff(['-ss', f'{t0:.2f}', '-t', f'{plen:.2f}', '-i', src, '-an', '-vf', scale_short(480) + ',fps=30,format=yuv420p', '-c:v', 'libx264', '-preset', 'slow', '-crf', '27', '-maxrate', '1500k', '-bufsize', '3000k', '-profile:v', 'main', '-movflags', '+faststart', prev])
        for kind, path in (('poster', poster), ('preview', prev), ('hd', hd)):
            print(f'  subiendo {kind} ({os.path.getsize(path) / 1048576:.1f} MB)', flush=True)
            upload(vid, kind, path, chunk)
        r = call('POST', fields={'action': 'done', 'id': vid, 'w': w, 'h': h})
        if not r.get('success'): raise RuntimeError(r.get('error', 'no se pudo terminar'))
        print('  ✓ listo', flush=True)

def main():
    r = call('GET', {'action': 'pending'})
    items = r.get('pending', [])
    chunk = max(256 * 1024, min(8 * 1048576, int(r.get('maxChunk', 2097152) * 0.9)))
    print(f'{len(items)} video(s) para convertir. Pedazos de {chunk / 1048576:.1f} MB.', flush=True)
    if not items: return
    if subprocess.run(['which', 'ffmpeg'], capture_output=True).returncode != 0:
        print('Instalando ffmpeg…', flush=True)
        subprocess.run('sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg', shell=True, check=True)
    fails = 0
    for it in items:
        if time.time() - START > TIME_BUDGET:
            print('Se terminó el tiempo de esta vuelta; sigue en la próxima.'); break
        try:
            convert(it, chunk)
        except Exception as e:
            fails += 1
            print(f'  ✗ {e}', flush=True)
            try: call('POST', fields={'action': 'fail', 'id': it['id'], 'error': str(e)[:300]})
            except Exception: pass
    if fails and fails == len(items): sys.exit(1)

if __name__ == '__main__':
    main()
