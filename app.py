# ═══════════════════════════════════════════════════════════════════════
# PlushMate Server v7.0 — Streaming de audio sin límite de duración
#
# Cambios vs v6.1:
#   - POST /stream/<token>/chunk  → recibe chunks de audio PCM crudo
#   - POST /stream/<token>/end    → finaliza stream y procesa
#   - POST /stream/<token>/abort  → cancela stream activo
#   - Chunks escritos a disco → sin límite de RAM ni de duración
#   - /process original se mantiene para la app móvil
#   - Amplificación x8 movida al servidor (menos trabajo para el ESP32)
#   - Limpieza automática de streams huérfanos cada 60s
# ═══════════════════════════════════════════════════════════════════════

import requests, os, tempfile, uuid, threading, time, struct, json, hashlib, re, io
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_file, Response
from pathlib import Path

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB por chunk

# ── Env vars ──────────────────────────────────────────────────────────
ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY', '').strip()
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '').strip()
SUPABASE_URL       = os.environ.get('SUPABASE_URL',       '').strip()
SUPABASE_KEY       = os.environ.get('SUPABASE_KEY',       '').strip()
SUPABASE_ANON_KEY  = os.environ.get('SUPABASE_ANON_KEY',  '').strip()
_server_url        = os.environ.get('SERVER_URL', 'http://localhost:5000').strip()
SERVER_URL         = f"https://{_server_url}" if not _server_url.startswith('http') else _server_url

_default_persona = (
    "Eres PlushMate, un peluche mágico e inteligente. "
    "Eres cálido, divertido y siempre positivo. "
    "Respondes de forma breve y natural, como si hablaras con un amigo cercano."
)

HISTORY_LIMIT = 20
AUDIO_DIR     = Path('/tmp/plushmate_audio')
AUDIO_DIR.mkdir(exist_ok=True)
STREAM_DIR    = Path('/tmp/plushmate_streams')
STREAM_DIR.mkdir(exist_ok=True)

# ── Audio en memoria para respuestas en vivo ──────────────────────────
audio_live_cache: dict = {}
audio_live_lock  = threading.Lock()

# ── Streams activos ───────────────────────────────────────────────────
# token → {file, path, lock, peak, total_bytes, created_at}
active_streams: dict = {}
streams_lock   = threading.Lock()

# ── Sesiones por usuario ──────────────────────────────────────────────
session_data: dict = {}
session_lock = threading.Lock()

pending_commands: dict = {}
pending_commands_lock = threading.Lock()

def get_session(user_id: str) -> dict:
    with session_lock:
        if user_id not in session_data:
            session_data[user_id] = {'history': [], 'interaction_count': 0}
        return session_data[user_id]

# ── Supabase helpers ──────────────────────────────────────────────────
def sb_headers(token=None):
    h = {'apikey': SUPABASE_KEY, 'Content-Type': 'application/json'}
    h['Authorization'] = f'Bearer {token}' if token else f'Bearer {SUPABASE_KEY}'
    return h

def sb_get(table, select='*', filter_str='', token=None):
    try:
        url  = f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
        if filter_str: url += f"&{filter_str}"
        r    = requests.get(url, headers=sb_headers(token), timeout=5)
        data = r.json()
        return data[0] if isinstance(data, list) and data else (data if not isinstance(data, list) else None)
    except: return None

def sb_get_all(table, select='*', filter_str='', token=None):
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
        if filter_str: url += f"&{filter_str}"
        r   = requests.get(url, headers=sb_headers(token), timeout=5)
        return r.json() if isinstance(r.json(), list) else []
    except: return []

def sb_patch(table, body, filter_str='', token=None):
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?{filter_str}"
        requests.patch(url, headers={**sb_headers(token), 'Prefer': 'return=minimal'},
                       json=body, timeout=5)
        return True
    except: return False

def sb_upsert(table, body, token=None):
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers={**sb_headers(token), 'Prefer': 'resolution=merge-duplicates'},
                      json=body, timeout=5)
        return True
    except: return False

# ── Auth helpers ──────────────────────────────────────────────────────
def verify_token(token: str):
    if not token: return None
    try:
        r = requests.get(f"{SUPABASE_URL}/auth/v1/user",
                         headers={'apikey': SUPABASE_ANON_KEY,
                                  'Authorization': f'Bearer {token}'},
                         timeout=5)
        if r.status_code != 200: return None
        user    = r.json()
        profile = sb_get('profiles', 'id,role', f'id=eq.{user["id"]}')
        role    = profile.get('role', 'user') if profile else 'user'
        return {'id': user['id'], 'email': user.get('email', ''), 'role': role}
    except: return None

def get_current_user():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '): return None
    return verify_token(auth[7:])

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user: return jsonify({'error': 'Unauthorized'}), 401
        return f(user, *args, **kwargs)
    return decorated

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or user['role'] not in ('admin', 'dev'):
            return jsonify({'error': 'Forbidden'}), 403
        return f(user, *args, **kwargs)
    return decorated

def verify_plush_request():
    token = request.headers.get('X-Plush-Token', '')
    if not token: return None
    return sb_get('plushes', '*', f'plush_token=eq.{token}')

# ── Auth endpoints ────────────────────────────────────────────────────
@app.route('/auth/signup', methods=['POST'])
def signup():
    data     = request.json or {}
    email    = data.get('email', '').strip()
    password = data.get('password', '')
    name     = data.get('name', '').strip()
    if not email or not password:
        return jsonify({'error': 'Email y contraseña requeridos'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Contraseña mínimo 6 caracteres'}), 400
    r = requests.post(f"{SUPABASE_URL}/auth/v1/signup",
                      headers={'apikey': SUPABASE_ANON_KEY, 'Content-Type': 'application/json'},
                      json={'email': email, 'password': password}, timeout=10)
    d = r.json()
    if r.status_code not in (200, 201) or 'error' in d:
        msg = d.get('error_description') or d.get('msg') or d.get('error') or 'Error al registrar'
        return jsonify({'error': msg}), 400
    user_id = d.get('user', {}).get('id') or d.get('id')
    if user_id and name:
        sb_patch('profiles', {'name': name}, f'id=eq.{user_id}')
    return jsonify({'token': d.get('access_token'), 'refresh_token': d.get('refresh_token'),
                    'user': {'id': user_id, 'email': email}})

@app.route('/auth/login', methods=['POST'])
def login():
    data     = request.json or {}
    email    = data.get('email', '').strip()
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'error': 'Email y contraseña requeridos'}), 400
    r = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={'apikey': SUPABASE_ANON_KEY, 'Content-Type': 'application/json'},
        json={'email': email, 'password': password}, timeout=10)
    d = r.json()
    if r.status_code != 200 or 'error' in d:
        msg = d.get('error_description') or d.get('msg') or 'Credenciales incorrectas'
        return jsonify({'error': msg}), 401
    user_id = d.get('user', {}).get('id')
    profile = sb_get('profiles', '*', f'id=eq.{user_id}') or {}
    return jsonify({
        'token':         d.get('access_token'),
        'refresh_token': d.get('refresh_token'),
        'user': {
            'id':     user_id,
            'email':  email,
            'name':   profile.get('name',   ''),
            'avatar': profile.get('avatar', '🐻'),
            'color':  profile.get('color',  '#8FA0CA'),
            'role':   profile.get('role',   'user'),
        }
    })

@app.route('/auth/refresh', methods=['POST'])
def refresh_token():
    data    = request.json or {}
    refresh = data.get('refresh_token', '')
    if not refresh: return jsonify({'error': 'No refresh token'}), 400
    r = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
        headers={'apikey': SUPABASE_ANON_KEY, 'Content-Type': 'application/json'},
        json={'refresh_token': refresh}, timeout=10)
    d = r.json()
    if r.status_code != 200:
        return jsonify({'error': 'Token expirado'}), 401
    return jsonify({'token': d.get('access_token'), 'refresh_token': d.get('refresh_token')})

@app.route('/auth/status', methods=['GET'])
def auth_status():
    try:
        r       = requests.get(f"{SUPABASE_URL}/rest/v1/pin?id=eq.1&select=hash",
                               headers=sb_headers(), timeout=5)
        data    = r.json()
        has_pin = bool(data and data[0].get('hash'))
    except:
        has_pin = False
    return jsonify({'has_pin': has_pin})

@app.route('/auth/setup', methods=['POST'])
def auth_setup():
    data        = request.json or {}
    new_pin     = data.get('new_pin', '')
    current_pin = data.get('current_pin', '')
    if not new_pin or len(new_pin) != 4 or not new_pin.isdigit():
        return jsonify({'error': 'PIN inválido'}), 400
    try:
        r      = requests.get(f"{SUPABASE_URL}/rest/v1/pin?id=eq.1&select=hash",
                              headers=sb_headers(), timeout=5)
        stored = (r.json() or [{}])[0].get('hash', '') if r.json() else ''
    except:
        stored = ''
    if stored:
        if not current_pin or hashlib.sha256(current_pin.encode()).hexdigest() != stored:
            return jsonify({'error': 'PIN actual incorrecto'}), 401
    new_hash = hashlib.sha256(new_pin.encode()).hexdigest()
    requests.post(f"{SUPABASE_URL}/rest/v1/pin",
                  headers={**sb_headers(), 'Prefer': 'resolution=merge-duplicates'},
                  json={'id': 1, 'hash': new_hash}, timeout=5)
    return jsonify({'ok': True})

@app.route('/auth/verify', methods=['POST'])
def auth_verify():
    data        = request.json or {}
    pin         = data.get('pin', '')
    device_id   = data.get('device_id', '')
    device_name = data.get('device_name', 'Dispositivo')
    user_id     = data.get('user_id', '')
    if not pin or len(pin) != 4 or not pin.isdigit():
        return jsonify({'error': 'PIN inválido'}), 400
    try:
        r      = requests.get(f"{SUPABASE_URL}/rest/v1/pin?id=eq.1&select=hash",
                              headers=sb_headers(), timeout=5)
        stored = (r.json() or [{}])[0].get('hash', '') if r.json() else ''
    except:
        stored = ''
    if not stored:
        return jsonify({'error': 'No hay PIN configurado'}), 400
    if hashlib.sha256(pin.encode()).hexdigest() != stored:
        return jsonify({'error': 'PIN incorrecto'}), 401
    if device_id and user_id:
        sb_upsert('devices', {'id': device_id, 'user_id': user_id, 'name': device_name,
                               'last_seen': datetime.now(timezone.utc).isoformat(),
                               'revoked': False})
    return jsonify({'ok': True})

@app.route('/auth/me', methods=['GET'])
@require_auth
def get_me(user):
    profile = sb_get('profiles', '*', f'id=eq.{user["id"]}') or {}
    plush   = sb_get('plushes', 'id,name,plush_token,paired_at', f'owner_id=eq.{user["id"]}')
    return jsonify({
        'id':     user['id'],
        'email':  user['email'],
        'name':   profile.get('name',   ''),
        'avatar': profile.get('avatar', '🐻'),
        'color':  profile.get('color',  '#8FA0CA'),
        'role':   user['role'],
        'plush':  plush,
    })

@app.route('/auth/checkin', methods=['POST'])
def auth_checkin():
    data        = request.json or {}
    device_id   = data.get('device_id', '')
    device_name = data.get('device_name', 'Dispositivo')
    user_id     = data.get('user_id', '')
    if not device_id or not user_id:
        return jsonify({'revoked': False})
    row = sb_get('devices', 'revoked', f'id=eq.{device_id}&user_id=eq.{user_id}')
    if row and row.get('revoked'):
        return jsonify({'revoked': True})
    sb_upsert('devices', {'id': device_id, 'user_id': user_id, 'name': device_name,
                           'last_seen': datetime.now(timezone.utc).isoformat(), 'revoked': False})
    return jsonify({'revoked': False})

# ── Plush pairing ─────────────────────────────────────────────────────
@app.route('/plush/pair', methods=['POST'])
@require_auth
def pair_plush(user):
    data        = request.json or {}
    plush_token = data.get('plush_token', '').strip().upper()
    if not plush_token:
        return jsonify({'error': 'Token requerido'}), 400
    plush = sb_get('plushes', '*', f'plush_token=eq.{plush_token}')
    if not plush:
        return jsonify({'error': 'QR no válido o peluche no encontrado'}), 404
    if plush.get('owner_id') and plush['owner_id'] != user['id']:
        return jsonify({'error': 'Este peluche ya está vinculado a otra cuenta'}), 409
    sb_patch('plushes', {
        'owner_id':  user['id'],
        'paired_at': datetime.now(timezone.utc).isoformat()
    }, f'plush_token=eq.{plush_token}')
    return jsonify({'ok': True, 'plush': {'id': plush['id'], 'name': plush.get('name', 'PlushMate')}})

@app.route('/plush/unpair', methods=['POST'])
@require_auth
def unpair_plush(user):
    sb_patch('plushes', {'owner_id': None, 'paired_at': None},
             f'owner_id=eq.{user["id"]}')
    return jsonify({'ok': True})

@app.route('/plush/config', methods=['GET', 'POST'])
@require_auth
def plush_config(user):
    plush = sb_get('plushes', '*', f'owner_id=eq.{user["id"]}')
    if not plush:
        return jsonify({'error': 'Sin peluche vinculado'}), 404
    if request.method == 'GET':
        return jsonify(plush)
    data   = request.json or {}
    update = {}
    for field in ('name', 'persona', 'voice_id', 'model', 'stt_language'):
        if field in data: update[field] = data[field]
    if update:
        sb_patch('plushes', update, f'owner_id=eq.{user["id"]}')
    return jsonify({'ok': True})

# ── Static files ──────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_file('static/index.html')

@app.route('/manifest.json')
def manifest():
    return send_file('static/manifest.json', mimetype='application/manifest+json')

@app.route('/static/icons/<path:filename>')
def static_icons(filename):
    from flask import send_from_directory
    icons_path = os.path.join(os.path.dirname(__file__), 'static', 'icons')
    if not os.path.exists(os.path.join(icons_path, filename)):
        return '', 404
    return send_from_directory(icons_path, filename)

@app.route('/health')
def health():
    with streams_lock:
        n_streams = len(active_streams)
    return jsonify({'status': 'ok', 'version': '7.0', 'active_streams': n_streams})

# ═══════════════════════════════════════════════════════════════════════
# STREAMING — tres endpoints que el ESP32 llama en secuencia:
#
#   1. POST /stream/<token>/chunk   → envía PCM crudo, sin límite
#   2. POST /stream/<token>/end     → finaliza y devuelve audio_url
#   3. POST /stream/<token>/abort   → cancela (botón suelto sin audio)
#
# El primer /chunk crea el stream automáticamente.
# Los datos van directo a disco — cero acumulación en RAM.
# ═══════════════════════════════════════════════════════════════════════

def _cleanup_stream(plush_token: str):
    with streams_lock:
        info = active_streams.pop(plush_token, None)
    if info:
        try:
            fh = info.get('file')
            if fh and not fh.closed:
                fh.close()
        except: pass
        try:
            path = info.get('path')
            if path and os.path.exists(path):
                os.unlink(path)
        except: pass

def _get_or_create_stream(plush_token: str) -> dict:
    with streams_lock:
        if plush_token not in active_streams:
            tmp = tempfile.NamedTemporaryFile(
                suffix='.pcm', delete=False,
                dir=str(STREAM_DIR), prefix=f"pm_"
            )
            active_streams[plush_token] = {
                'file':        tmp,
                'path':        tmp.name,
                'lock':        threading.Lock(),
                'peak':        0,
                'total_bytes': 0,
                'created_at':  time.time(),
            }
            print(f"[stream] Nuevo: {plush_token} → {tmp.name}", flush=True)
        return active_streams[plush_token]


@app.route('/stream/<plush_token>/chunk', methods=['POST'])
def stream_chunk(plush_token):
    """Recibe un chunk de PCM crudo (sin header WAV) y lo escribe a disco."""
    plush = sb_get('plushes', 'owner_id,plush_token', f'plush_token=eq.{plush_token}')
    if not plush:
        return jsonify({'error': 'Token inválido'}), 401

    chunk = request.data
    if not chunk:
        return jsonify({'error': 'Chunk vacío'}), 400

    info = _get_or_create_stream(plush_token)

    with info['lock']:
        # Calcular amplitud pico sin amplificar (la amplificación se hace en /end)
        n = len(chunk) // 2
        if n > 0:
            samples    = struct.unpack(f'<{n}h', chunk[:n*2])
            peak_chunk = max(abs(s) for s in samples)
            if peak_chunk > info['peak']:
                info['peak'] = peak_chunk

        info['file'].write(chunk)
        info['file'].flush()
        info['total_bytes'] += len(chunk)

    return jsonify({
        'ok':          True,
        'total_bytes': info['total_bytes'],
        'peak':        info['peak'],
    }), 200


@app.route('/stream/<plush_token>/end', methods=['POST'])
def stream_end(plush_token):
    """Cierra el stream, amplifica, construye WAV, procesa y devuelve audio_url."""
    import traceback

    plush = sb_get('plushes', '*', f'plush_token=eq.{plush_token}')
    if not plush:
        return jsonify({'error': 'Token inválido'}), 401

    with streams_lock:
        info = active_streams.get(plush_token)
    if not info:
        return jsonify({'error': 'Sin stream activo'}), 400

    try:
        # Cerrar archivo
        with info['lock']:
            info['file'].flush()
            info['file'].close()
            pcm_path    = info['path']
            peak_raw    = info['peak']
            total_bytes = info['total_bytes']

        # Limpiar entrada del dict (ya tenemos los datos en disco)
        with streams_lock:
            active_streams.pop(plush_token, None)

        print(f"[stream] END {plush_token}: {total_bytes}B peak_raw={peak_raw}", flush=True)

        # Validaciones
        if total_bytes < 3200:
            try: os.unlink(pcm_path)
            except: pass
            return jsonify({'error': 'Audio demasiado corto — habla más tiempo'}), 400

        if peak_raw < 38:   # sin amplificar x8, threshold es 300/8 ≈ 38
            try: os.unlink(pcm_path)
            except: pass
            return jsonify({'error': 'Audio demasiado silencioso'}), 400

        # Leer PCM, amplificar x8 y construir WAV
        with open(pcm_path, 'rb') as f:
            raw_pcm = f.read()
        try: os.unlink(pcm_path)
        except: pass

        n       = len(raw_pcm) // 2
        samples = struct.unpack(f'<{n}h', raw_pcm[:n*2])
        amp     = struct.pack(
            f'<{n}h',
            *[max(-32768, min(32767, s * 8)) for s in samples]
        )

        wav_path = str(STREAM_DIR / f"pm_{plush_token}_{uuid.uuid4().hex[:8]}.wav")
        with open(wav_path, 'wb') as wf:
            data_len = len(amp)
            wf.write(b'RIFF')
            wf.write(struct.pack('<I', 36 + data_len))
            wf.write(b'WAVE')
            wf.write(b'fmt ')
            wf.write(struct.pack('<I', 16))
            wf.write(struct.pack('<H', 1))       # PCM
            wf.write(struct.pack('<H', 1))       # mono
            wf.write(struct.pack('<I', 16000))   # sample rate
            wf.write(struct.pack('<I', 32000))   # byte rate
            wf.write(struct.pack('<H', 2))       # block align
            wf.write(struct.pack('<H', 16))      # bits/sample
            wf.write(b'data')
            wf.write(struct.pack('<I', data_len))
            wf.write(amp)

        # ── STT → LLM → TTS ──────────────────────────────────────────
        owner_id = plush.get('owner_id')
        if not owner_id:
            try: os.unlink(wav_path)
            except: pass
            return jsonify({'error': 'Peluche sin cuenta vinculada'}), 403

        try:
            transcript = stt(wav_path, plush.get('stt_language', 'es'))
        finally:
            try: os.unlink(wav_path)
            except: pass

        if not transcript:
            return jsonify({'error': 'No se entendió el audio'}), 400

        sess = get_session(owner_id)
        sess['history'].append({'role': 'user', 'content': transcript, 'audio_url': None})

        ai_text = chat_with_memory(owner_id, plush)
        if not ai_text:
            sess['history'].pop()
            return jsonify({'error': 'Sin respuesta del modelo'}), 500

        display_text = re.sub(r'\[.*?\]', '', ai_text).strip()
        sess['history'].append({'role': 'assistant', 'content': display_text, 'audio_url': None})
        if len(sess['history']) > HISTORY_LIMIT:
            sess['history'] = sess['history'][-HISTORY_LIMIT:]

        sess['interaction_count'] = sess.get('interaction_count', 0) + 1
        if sess['interaction_count'] % 5 == 0:
            threading.Thread(target=update_summary,
                             args=(owner_id, sess['history'], plush),
                             daemon=True).start()

        # Limpiar audio huérfano
        with audio_live_lock:
            if plush_token in audio_live_cache:
                del audio_live_cache[plush_token]

        audio_url = f"{SERVER_URL}/audio/live/{plush_token}"

        def run_tts():
            try:
                audio_bytes = tts(ai_text, plush.get('voice_id', ''))
                with audio_live_lock:
                    audio_live_cache[plush_token] = audio_bytes
                print(f"[TTS] Listo {plush_token} ({len(audio_bytes)}B)", flush=True)
                filename = f"{uuid.uuid4()}.mp3"
                path     = AUDIO_DIR / filename
                with open(str(path), 'wb') as fh:
                    fh.write(audio_bytes)
                files = sorted(AUDIO_DIR.glob('*.mp3'), key=lambda p: p.stat().st_mtime)
                for old in files[:-50]:
                    try: old.unlink()
                    except: pass
                with session_lock:
                    h = session_data.get(owner_id, {}).get('history', [])
                    for msg in reversed(h):
                        if msg['role'] == 'assistant' and msg['audio_url'] is None:
                            msg['audio_url'] = f"{SERVER_URL}/audio/{filename}"
                            break
            except Exception as e:
                print(f"[TTS] ERROR: {e}", flush=True)

        threading.Thread(target=run_tts, daemon=True).start()

        return jsonify({
            'transcript': transcript,
            'response':   display_text,
            'audio_url':  audio_url,
        })

    except Exception as e:
        tb = traceback.format_exc()
        print(f"\n🚨 ERROR /stream/end:\n{tb}\n", flush=True)
        _cleanup_stream(plush_token)
        return jsonify({'error': str(e)}), 500


@app.route('/stream/<plush_token>/abort', methods=['POST'])
def stream_abort(plush_token):
    """Cancela y limpia un stream activo."""
    _cleanup_stream(plush_token)
    print(f"[stream] Abortado: {plush_token}", flush=True)
    return jsonify({'ok': True}), 200


# ── Limpieza de streams huérfanos (>2 min) ───────────────────────────
def _cleanup_stale():
    while True:
        time.sleep(60)
        now   = time.time()
        stale = []
        with streams_lock:
            for token, info in active_streams.items():
                if now - info.get('created_at', now) > 120:
                    stale.append(token)
        for token in stale:
            print(f"[stream] Limpiando huérfano: {token}", flush=True)
            _cleanup_stream(token)

threading.Thread(target=_cleanup_stale, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════
# AUDIO EN VIVO
# ═══════════════════════════════════════════════════════════════════════

@app.route('/audio/live/<plush_token>')
def serve_live_audio(plush_token):
    timeout_s = 12.0
    step      = 0.05
    waited    = 0.0
    while waited < timeout_s:
        with audio_live_lock:
            data = audio_live_cache.get(plush_token)
        if data is not None:
            with audio_live_lock:
                del audio_live_cache[plush_token]
            return Response(
                io.BytesIO(data),
                mimetype='audio/mpeg',
                headers={
                    'Content-Length': str(len(data)),
                    'Cache-Control':  'no-store',
                    'Accept-Ranges':  'bytes',
                }
            )
        time.sleep(step)
        waited += step
    return jsonify({'error': 'TTS timeout'}), 504


@app.route('/audio/<filename>')
def serve_audio(filename):
    path = AUDIO_DIR / filename
    if not path.exists():
        return jsonify({'error': 'Not found'}), 404
    return send_file(str(path), mimetype='audio/mpeg')


# ═══════════════════════════════════════════════════════════════════════
# /process — pipeline original (mantener para app móvil)
# ═══════════════════════════════════════════════════════════════════════

@app.route('/process', methods=['POST'])
def process_audio():
    import traceback
    try:
        plush = verify_plush_request()
        if not plush:
            return jsonify({'error': 'Plush token inválido'}), 401
        owner_id    = plush.get('owner_id')
        plush_token = plush.get('plush_token', '')
        if not owner_id:
            return jsonify({'error': 'Peluche no vinculado'}), 403
        wav_data = request.data
        if len(wav_data) < 44:
            return jsonify({'error': 'Audio inválido'}), 400
        try:
            samples = struct.unpack('<' + 'h' * ((len(wav_data) - 44) // 2), wav_data[44:])
            peak    = max(abs(s) for s in samples) if samples else 0
            if peak < 300:
                return jsonify({'error': 'Audio demasiado silencioso'}), 400
        except: pass
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(wav_data)
            tmp_path = f.name
        try:
            transcript = stt(tmp_path, plush.get('stt_language', 'es'))
            if not transcript:
                return jsonify({'error': 'No se entendió el audio'}), 400
            sess = get_session(owner_id)
            sess['history'].append({'role': 'user', 'content': transcript, 'audio_url': None})
            ai_text = chat_with_memory(owner_id, plush)
            if not ai_text:
                sess['history'].pop()
                return jsonify({'error': 'Sin respuesta'}), 500
            display_text = re.sub(r'\[.*?\]', '', ai_text).strip()
            sess['history'].append({'role': 'assistant', 'content': display_text, 'audio_url': None})
            if len(sess['history']) > HISTORY_LIMIT:
                sess['history'] = sess['history'][-HISTORY_LIMIT:]
            sess['interaction_count'] = sess.get('interaction_count', 0) + 1
            if sess['interaction_count'] % 5 == 0:
                threading.Thread(target=update_summary,
                                 args=(owner_id, sess['history'], plush), daemon=True).start()
            with audio_live_lock:
                if plush_token in audio_live_cache:
                    del audio_live_cache[plush_token]
            audio_url = f"{SERVER_URL}/audio/live/{plush_token}"
            def run_tts_bg():
                try:
                    audio_bytes = tts(ai_text, plush.get('voice_id', ''))
                    with audio_live_lock:
                        audio_live_cache[plush_token] = audio_bytes
                    filename = f"{uuid.uuid4()}.mp3"
                    path     = AUDIO_DIR / filename
                    with open(str(path), 'wb') as fh:
                        fh.write(audio_bytes)
                    files = sorted(AUDIO_DIR.glob('*.mp3'), key=lambda p: p.stat().st_mtime)
                    for old in files[:-50]:
                        try: old.unlink()
                        except: pass
                    with session_lock:
                        h = session_data.get(owner_id, {}).get('history', [])
                        for msg in reversed(h):
                            if msg['role'] == 'assistant' and msg['audio_url'] is None:
                                msg['audio_url'] = f"{SERVER_URL}/audio/{filename}"
                                break
                except Exception as e:
                    print(f"[TTS bg] ERROR: {e}", flush=True)
            threading.Thread(target=run_tts_bg, daemon=True).start()
            return jsonify({'transcript': transcript, 'response': display_text, 'audio_url': audio_url})
        finally:
            try: os.unlink(tmp_path)
            except: pass
    except Exception as e:
        print(f"\n🚨 ERROR /process:\n{traceback.format_exc()}\n", flush=True)
        return jsonify({'error': str(e)}), 500


# ── Memory ────────────────────────────────────────────────────────────
@app.route('/memory', methods=['GET'])
@require_auth
def get_memory(user):
    row = sb_get('memory', '*', f'user_id=eq.{user["id"]}')
    if not row:
        sb_upsert('memory', {'user_id': user['id'], 'summary': '',
                              'updated_at': datetime.now(timezone.utc).isoformat()})
        row = {}
    summary   = row.get('summary', '')
    plush     = sb_get('plushes', '*', f'owner_id=eq.{user["id"]}')
    sess      = get_session(user['id'])
    formatted = format_memory(summary, user['id'], plush)
    return jsonify({
        'summary':           summary,
        'summary_formatted': formatted,
        'history':           sess['history'],
        'history_len':       len(sess['history']),
    })

@app.route('/memory', methods=['DELETE'])
@require_auth
def clear_memory(user):
    sess = get_session(user['id'])
    sess['history']           = []
    sess['interaction_count'] = 0
    sb_upsert('memory', {'user_id': user['id'], 'summary': '',
                          'updated_at': datetime.now(timezone.utc).isoformat()})
    return jsonify({'status': 'cleared'})

# ── Profile ───────────────────────────────────────────────────────────
@app.route('/profile', methods=['GET'])
@require_auth
def get_profile(user):
    row = sb_get('profiles', '*', f'id=eq.{user["id"]}') or {}
    return jsonify({'name': row.get('name', ''), 'avatar': row.get('avatar', '🐻'),
                    'color': row.get('color', '#8FA0CA')})

@app.route('/profile', methods=['POST'])
@require_auth
def set_profile(user):
    data = request.json or {}
    body = {k: data[k] for k in ('name', 'avatar', 'color') if k in data}
    if body: sb_patch('profiles', body, f'id=eq.{user["id"]}')
    return jsonify({'ok': True})

# ── Devices ───────────────────────────────────────────────────────────
@app.route('/devices', methods=['GET'])
@require_auth
def list_devices(user):
    return jsonify(sb_get_all('devices', '*',
                              f'user_id=eq.{user["id"]}&revoked=eq.false&order=last_seen.desc'))

@app.route('/devices/<device_id>', methods=['DELETE'])
@require_auth
def revoke_device(user, device_id):
    sb_patch('devices', {'revoked': True}, f'id=eq.{device_id}&user_id=eq.{user["id"]}')
    return jsonify({'ok': True})

# ── Command queue ─────────────────────────────────────────────────────
@app.route('/command', methods=['GET'])
def get_command():
    plush_token = request.headers.get('X-Plush-Token', '')
    if not plush_token:
        return jsonify({'action': 'none'})
    with pending_commands_lock:
        cmd = pending_commands.pop(plush_token, None)
    return jsonify(cmd if cmd else {'action': 'none'})

@app.route('/command', methods=['POST'])
@require_auth
def set_command(user):
    data   = request.json or {}
    action = data.get('action')
    valid  = ('activate', 'stop', 'wifi_change', 'volume_set', 'ap_mode',
              'play_audio', 'scan_networks')
    if action not in valid:
        return jsonify({'error': 'Acción inválida'}), 400
    plush = sb_get('plushes', 'plush_token', f'owner_id=eq.{user["id"]}')
    if not plush:
        return jsonify({'error': 'Sin peluche vinculado'}), 404
    with pending_commands_lock:
        pending_commands[plush['plush_token']] = data
    return jsonify({'ok': True})

@app.route('/wifi', methods=['POST'])
@require_auth
def set_wifi(user):
    data     = request.json or {}
    ssid     = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    if not ssid: return jsonify({'error': 'SSID requerido'}), 400
    plush = sb_get('plushes', 'plush_token', f'owner_id=eq.{user["id"]}')
    if not plush: return jsonify({'error': 'Sin peluche vinculado'}), 404
    with pending_commands_lock:
        pending_commands[plush['plush_token']] = {
            'action': 'wifi_change', 'ssid': ssid, 'password': password
        }
    return jsonify({'status': 'queued'})

available_networks: dict = {}

@app.route('/networks/scan', methods=['POST'])
@require_auth
def networks_scan(user):
    plush = sb_get('plushes', 'plush_token', f'owner_id=eq.{user["id"]}')
    if not plush: return jsonify({'error': 'Sin peluche'}), 404
    with pending_commands_lock:
        pending_commands[plush['plush_token']] = {'action': 'scan_networks'}
    return jsonify({'status': 'scanning'})

@app.route('/networks/results', methods=['GET'])
@require_auth
def networks_results(user):
    plush = sb_get('plushes', 'plush_token', f'owner_id=eq.{user["id"]}')
    if not plush: return jsonify({'status': 'no_plush', 'networks': []}), 404
    nets  = available_networks.get(plush['plush_token'], [])
    return jsonify({'status': 'ready' if nets else 'waiting', 'networks': nets})

@app.route('/networks/results', methods=['POST'])
def networks_results_post():
    token = request.headers.get('X-Plush-Token', '')
    if not token: return jsonify({'error': 'No token'}), 401
    available_networks[token] = (request.json or {}).get('networks', [])
    return jsonify({'ok': True})

# ── Admin ─────────────────────────────────────────────────────────────
@app.route('/admin/users', methods=['GET'])
@require_admin
def admin_list_users(user):
    try:
        r        = requests.get(f"{SUPABASE_URL}/auth/v1/admin/users",
                                headers={'apikey': SUPABASE_KEY,
                                         'Authorization': f'Bearer {SUPABASE_KEY}'},
                                timeout=10)
        users    = r.json().get('users', [])
        profiles = sb_get_all('profiles', '*')
        pmap     = {p['id']: p for p in profiles}
        return jsonify([{
            'id': u['id'], 'email': u.get('email'),
            'role': pmap.get(u['id'], {}).get('role', 'user'),
            'name': pmap.get(u['id'], {}).get('name', ''),
            'created_at': u.get('created_at'),
        } for u in users])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/users/<user_id>/role', methods=['POST'])
@require_admin
def admin_set_role(user, user_id):
    data = request.json or {}
    role = data.get('role', 'user')
    if role not in ('user', 'admin', 'dev'):
        return jsonify({'error': 'Rol inválido'}), 400
    sb_upsert('profiles', {'id': user_id, 'role': role})
    return jsonify({'ok': True})

@app.route('/admin/logs', methods=['GET'])
@require_admin
def admin_logs(user):
    with session_lock:
        stats = [{'user_id': uid[:8], 'messages': len(s['history']),
                  'interactions': s.get('interaction_count', 0)}
                 for uid, s in session_data.items()]
    with audio_live_lock:
        pending_audio = list(audio_live_cache.keys())
    with streams_lock:
        active = {t: {'bytes': i['total_bytes'], 'peak': i['peak']}
                  for t, i in active_streams.items()}
    return jsonify({
        'active_sessions':  len(session_data),
        'sessions':         stats,
        'pending_commands': len(pending_commands),
        'pending_audio':    pending_audio,
        'active_streams':   active,
    })

@app.route('/admin/plushes', methods=['GET'])
@require_admin
def admin_plushes(user):
    return jsonify(sb_get_all('plushes', '*'))

# ═══════════════════════════════════════════════════════════════════════
# AI Pipeline
# ═══════════════════════════════════════════════════════════════════════

def extract_text(msg: dict) -> str:
    text = msg.get('content') or msg.get('reasoning') or ''
    if not text and msg.get('reasoning_details'):
        for rd in msg['reasoning_details']:
            if rd.get('text'): return rd['text']
    if not text:
        m = re.search(r'<think>(.*?)</think>', str(msg), re.DOTALL)
        if m: return m.group(1).strip()
    return text.strip()

def format_memory(summary: str, user_id: str, plush) -> list:
    if not summary or not summary.strip(): return []
    try:
        model = (plush.get('model', 'arcee-ai/trinity-large-preview:free')
                 if plush else 'arcee-ai/trinity-large-preview:free')
        r     = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                     'Content-Type': 'application/json'},
            json={'model': model, 'max_tokens': 400, 'messages': [{'role': 'user', 'content':
                f'Convierte este resumen en JSON con secciones. Solo JSON, sin markdown.\n'
                f'Formato: [{{"icon":"emoji","title":"Título","items":["dato1","dato2"]}}]\n'
                f'Resumen: {summary}'}]}, timeout=10)
        msg = r.json()['choices'][0]['message']
        raw = extract_text(msg)
        raw = re.sub(r'^```json|^```|```$', '', raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except:
        sentences = [s.strip() for s in re.split(r'[.!?]+', summary) if s.strip()]
        return [{"icon": "🧠", "title": "Lo que recuerdo", "items": sentences}]

def stt(wav_path: str, language: str = 'es') -> str:
    with open(wav_path, 'rb') as f:
        audio_data = f.read()
    r = requests.post(
        'https://api.elevenlabs.io/v1/speech-to-text',
        headers={'xi-api-key': ELEVENLABS_API_KEY},
        files={'file': ('audio.wav', audio_data, 'audio/wav')},
        data={'model_id': 'scribe_v1', 'tag_audio_events': 'false',
              'diarize': 'false', 'language_code': language},
        timeout=60)
    try:    return r.json().get('text', '').strip()
    except: return ''

def chat_with_memory(user_id: str, plush) -> str:
    sess    = get_session(user_id)
    row     = sb_get('memory', 'summary', f'user_id=eq.{user_id}') or {}
    summary = row.get('summary', '')
    persona = (plush.get('persona', '') if plush else '') or _default_persona
    brevity = (
        "\n\nIMPORTANTE: Estás hablando en voz alta con un niño. "
        "Responde SIEMPRE en máximo 2 oraciones cortas y naturales. "
        "Usa lenguaje sencillo, cálido y conversacional. "
        "Nunca uses listas, viñetas ni texto formateado. "
        "Si no sabes algo, di que no sabes de forma amigable en una oración."
    )
    system_content = persona + brevity
    if summary:
        system_content += f"\n\nRecuerdas esto del usuario:\n{summary}"
    model    = (plush.get('model', 'arcee-ai/trinity-large-preview:free')
                if plush else 'arcee-ai/trinity-large-preview:free')
    messages = [{'role': 'system', 'content': system_content}] + [
        {'role': m['role'], 'content': m['content']}
        for m in sess['history'][-10:]
    ]
    print(f"🧠 LLM: {model} | historial: {len(sess['history'])} msgs", flush=True)
    r    = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                 'Content-Type': 'application/json'},
        json={'model': model, 'max_tokens': 80, 'messages': messages},
        timeout=30)
    data = r.json()
    if not data.get('choices'):
        print(f"🚨 ERROR OPENROUTER: {data}", flush=True)
        return ''
    return extract_text(data['choices'][0]['message'])

def update_summary(user_id: str, history: list, plush):
    try:
        row   = sb_get('memory', 'summary', f'user_id=eq.{user_id}')
        if not row:
            sb_upsert('memory', {'user_id': user_id, 'summary': '',
                                  'updated_at': datetime.now(timezone.utc).isoformat()})
        old   = (row or {}).get('summary', '')
        conv  = '\n'.join([
            f"{'Usuario' if m['role']=='user' else 'PlushMate'}: {m['content']}"
            for m in history[-10:]
        ])
        model = (plush.get('model', 'arcee-ai/trinity-large-preview:free')
                 if plush else 'arcee-ai/trinity-large-preview:free')
        r     = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                     'Content-Type': 'application/json'},
            json={'model': model, 'max_tokens': 200, 'messages': [{'role': 'user', 'content':
                f'Resumen existente: {old}\nConversación nueva:\n{conv}\n'
                f'Actualiza el resumen en 3-5 oraciones. Solo el resumen.'}]},
            timeout=15)
        new_summary = extract_text(r.json()['choices'][0]['message'])
        if new_summary:
            sb_upsert('memory', {'user_id': user_id, 'summary': new_summary,
                                  'updated_at': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f"[summary] ERROR: {e}", flush=True)

def tts(text: str, voice_id: str = '') -> bytes:
    vid = voice_id or os.environ.get('ELEVENLABS_VOICE_ID', 'aaf0KU31jmlzVPqltvJY')
    r   = requests.post(
        f'https://api.elevenlabs.io/v1/text-to-speech/{vid}/stream',
        headers={'xi-api-key': ELEVENLABS_API_KEY, 'Content-Type': 'application/json'},
        json={'text': text, 'model_id': 'eleven_v3',
              'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75},
              'optimize_streaming_latency': 3},
        stream=True, timeout=30)
    if r.status_code != 200:
        raise Exception(f"TTS error {r.status_code}: {r.text[:200]}")
    buf = io.BytesIO()
    for chunk in r.iter_content(chunk_size=4096):
        if chunk: buf.write(chunk)
    return buf.getvalue()

# ── Keep-alive ────────────────────────────────────────────────────────
def keep_alive():
    while True:
        time.sleep(300)
        try: requests.get(f"{SERVER_URL}/health", timeout=5)
        except: pass

threading.Thread(target=keep_alive, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
