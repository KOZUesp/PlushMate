# ═══════════════════════════════════════════════════════════════════════
# PlushMate Server v5.0 — Multi-usuario con Supabase Auth
# ═══════════════════════════════════════════════════════════════════════
import requests, os, tempfile, uuid, threading, time, struct, json, hashlib, re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_file
from pathlib import Path

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 # 10 MB limit

# ── Env vars ──────────────────────────────────────────────────────────
ELEVENLABS_API_KEY  = os.environ.get('ELEVENLABS_API_KEY', '').strip()
OPENROUTER_API_KEY  = os.environ.get('OPENROUTER_API_KEY', '').strip()
SUPABASE_URL        = os.environ.get('SUPABASE_URL', '').strip()
SUPABASE_KEY        = os.environ.get('SUPABASE_KEY', '').strip()
SUPABASE_ANON_KEY   = os.environ.get('SUPABASE_ANON_KEY', '').strip()
_server_url         = os.environ.get('SERVER_URL', 'http://localhost:5000').strip()
SERVER_URL          = f"https://{_server_url}" if not _server_url.startswith('http') else _server_url

_default_persona = (
    "Eres PlushMate, un peluche mágico e inteligente. "
    "Eres cálido, divertido y siempre positivo. "
    "Respondes de forma breve y natural, como si hablaras con un amigo cercano."
)

HISTORY_LIMIT = 20
AUDIO_DIR = Path('/tmp/plushmate_audio')
AUDIO_DIR.mkdir(exist_ok=True)

# ── Per-session state (por user_id) ───────────────────────────────────
session_data = {}
session_lock = threading.Lock()
pending_commands = {}
pending_commands_lock = threading.Lock()

def get_session(user_id: str) -> dict:
    with session_lock:
        if user_id not in session_data:
            session_data[user_id] = {'history': [], 'interaction_count': 0}
        return session_data[user_id]

# ── Supabase helpers ──────────────────────────────────────────────────
def sb_headers(token=None):
    """Service role key bypasses RLS."""
    h = {'apikey': SUPABASE_KEY, 'Content-Type': 'application/json'}
    h['Authorization'] = f'Bearer {token}' if token else f'Bearer {SUPABASE_KEY}'
    return h

def sb_get(table, select='*', filter_str='', token=None):
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
        if filter_str: url += f"&{filter_str}"
        r = requests.get(url, headers=sb_headers(token), timeout=5)
        data = r.json()
        return data[0] if isinstance(data, list) and data else (data if not isinstance(data, list) else None)
    except: return None

def sb_get_all(table, select='*', filter_str='', token=None):
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
        if filter_str: url += f"&{filter_str}"
        r = requests.get(url, headers=sb_headers(token), timeout=5)
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
                        headers={'apikey': SUPABASE_ANON_KEY, 'Authorization': f'Bearer {token}'},
                        timeout=5)
        if r.status_code != 200: return None
        user = r.json()
        profile = sb_get('profiles', 'id,role', f'id=eq.{user["id"]}')
        role = profile.get('role', 'user') if profile else 'user'
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
    data = request.json or {}
    email = data.get('email', '').strip()
    password = data.get('password', '')
    name = data.get('name', '').strip()
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
    data = request.json or {}
    email = data.get('email', '').strip()
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
        'token': d.get('access_token'),
        'refresh_token': d.get('refresh_token'),
        'user': {'id': user_id, 'email': email,
                 'name': profile.get('name',''), 'avatar': profile.get('avatar','🐻'),
                 'color': profile.get('color','#8FA0CA'), 'role': profile.get('role','user')}
    })

@app.route('/auth/refresh', methods=['POST'])
def refresh_token():
    data = request.json or {}
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
        r = requests.get(f"{SUPABASE_URL}/rest/v1/pin?id=eq.1&select=hash",
                        headers=sb_headers(), timeout=5)
        data = r.json()
        has_pin = bool(data and data[0].get('hash'))
    except:
        has_pin = False
    return jsonify({'has_pin': has_pin})

@app.route('/auth/setup', methods=['POST'])
def auth_setup():
    data = request.json or {}
    new_pin = data.get('new_pin', '')
    current_pin = data.get('current_pin', '')
    if not new_pin or len(new_pin) != 4 or not new_pin.isdigit():
        return jsonify({'error': 'PIN inválido'}), 400
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/pin?id=eq.1&select=hash",
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
    data = request.json or {}
    pin = data.get('pin', '')
    device_id = data.get('device_id', '')
    device_name = data.get('device_name', 'Dispositivo')
    user_id = data.get('user_id', '')
    if not pin or len(pin) != 4 or not pin.isdigit():
        return jsonify({'error': 'PIN inválido'}), 400
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/pin?id=eq.1&select=hash",
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
                               'last_seen': datetime.now(timezone.utc).isoformat(), 'revoked': False})
    return jsonify({'ok': True})

@app.route('/auth/me', methods=['GET'])
@require_auth
def get_me(user):
    profile = sb_get('profiles', '*', f'id=eq.{user["id"]}') or {}
    plush = sb_get('plushes', 'id,name,plush_token,paired_at', f'owner_id=eq.{user["id"]}')
    return jsonify({
        'id': user['id'], 'email': user['email'],
        'name': profile.get('name',''), 'avatar': profile.get('avatar','🐻'),
        'color': profile.get('color','#8FA0CA'), 'role': user['role'],
        'plush': plush
    })

@app.route('/auth/checkin', methods=['POST'])
def auth_checkin():
    data = request.json or {}
    device_id = data.get('device_id', '')
    device_name = data.get('device_name', 'Dispositivo')
    user_id = data.get('user_id', '')
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
    data = request.json or {}
    plush_token = data.get('plush_token', '').strip().upper()
    if not plush_token:
        return jsonify({'error': 'Token requerido'}), 400
    plush = sb_get('plushes', '*', f'plush_token=eq.{plush_token}')
    if not plush:
        return jsonify({'error': 'QR no válido o peluche no encontrado'}), 404
    if plush.get('owner_id') and plush['owner_id'] != user['id']:
        return jsonify({'error': 'Este peluche ya está vinculado a otra cuenta'}), 409
    sb_patch('plushes', {
        'owner_id': user['id'],
        'paired_at': datetime.now(timezone.utc).isoformat()
    }, f'plush_token=eq.{plush_token}')
    return jsonify({'ok': True, 'plush': {'id': plush['id'], 'name': plush.get('name','PlushMate')}})

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
    data = request.json or {}
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
    return jsonify({'status': 'ok', 'version': '5.0'})

# ── Audio processing ──────────────────────────────────────────────────

@app.route('/process', methods=['POST'])
def process_audio():
    import traceback  # Aseguramos tener traceback disponible
    
    try:
        plush = verify_plush_request()
        if not plush:
            return jsonify({'error': 'Plush token inválido'}), 401
        owner_id = plush.get('owner_id')
        if not owner_id:
            return jsonify({'error': 'Peluche no vinculado a ninguna cuenta'}), 403

        wav_data = request.data
        if len(wav_data) < 44:
            return jsonify({'error': 'Audio inválido'}), 400

        # Silence filter
        try:
            samples = struct.unpack('<' + 'h' * ((len(wav_data) - 44) // 2), wav_data[44:])
            peak = max(abs(s) for s in samples) if samples else 0
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

            display_text = re.sub(r'\\[.*?\\]', '', ai_text).strip()
            sess['history'].append({'role': 'assistant', 'content': display_text, 'audio_url': None})
            if len(sess['history']) > HISTORY_LIMIT:
                sess['history'] = sess['history'][-HISTORY_LIMIT:]

            sess['interaction_count'] = sess.get('interaction_count', 0) + 1
            if sess['interaction_count'] % 5 == 0:
                threading.Thread(target=update_summary, args=(owner_id, sess['history'], plush), daemon=True).start()

            audio_filename = tts(ai_text, plush.get('voice_id', ''))
            audio_url = f"{SERVER_URL}/audio/{audio_filename}"
            sess['history'][-1]['audio_url'] = audio_url

            return jsonify({'transcript': transcript, 'response': display_text, 'audio_url': audio_url})
        finally:
            try: os.unlink(tmp_path)
            except: pass

    except Exception as e:
        # 1. Obtenemos el texto exacto del error y la línea donde ocurrió
        error_real = traceback.format_exc()
        
        # 2. Forzamos la salida en Railway AHORA MISMO
        print("\n🚨 === ERROR CRITICO EN /process ===", flush=True)
        print(error_real, flush=True)
        print("======================================\n", flush=True)
        
        # 3. Lo devolvemos en el JSON (Status 500)
        return jsonify({
            'status': 'error',
            'error': str(e),
            'traceback': error_real
        }), 500
        
# ── Memory ────────────────────────────────────────────────────────────

@app.route('/memory', methods=['GET'])
@require_auth
def get_memory(user):
    row = sb_get('memory', '*', f'user_id=eq.{user["id"]}')
    if not row:
        sb_upsert('memory', {'user_id': user['id'], 'summary': '',
                              'updated_at': datetime.now(timezone.utc).isoformat()})
        row = {}
    summary = row.get('summary', '')
    plush = sb_get('plushes', '*', f'owner_id=eq.{user["id"]}')
    sess = get_session(user['id'])
    formatted = format_memory(summary, user['id'], plush)
    return jsonify({
        'summary': summary,
        'summary_formatted': formatted,
        'history': sess['history'],
        'history_len': len(sess['history'])
    })

@app.route('/memory', methods=['DELETE'])
@require_auth
def clear_memory(user):
    sess = get_session(user['id'])
    sess['history'] = []
    sess['interaction_count'] = 0
    sb_upsert('memory', {'user_id': user['id'], 'summary': '',
                          'updated_at': datetime.now(timezone.utc).isoformat()})
    return jsonify({'status': 'cleared'})

# ── Profile ───────────────────────────────────────────────────────────

@app.route('/profile', methods=['GET'])
@require_auth
def get_profile(user):
    row = sb_get('profiles', '*', f'id=eq.{user["id"]}') or {}
    return jsonify({'name': row.get('name',''), 'avatar': row.get('avatar','🐻'),
                    'color': row.get('color','#8FA0CA')})

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
    devices = sb_get_all('devices', '*',
        f'user_id=eq.{user["id"]}&revoked=eq.false&order=last_seen.desc')
    return jsonify(devices)

@app.route('/devices/<device_id>', methods=['DELETE'])
@require_auth
def revoke_device(user, device_id):
    sb_patch('devices', {'revoked': True},
             f'id=eq.{device_id}&user_id=eq.{user["id"]}')
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
    data = request.json or {}
    action = data.get('action')
    valid = ('activate', 'stop', 'wifi_change', 'volume_set', 'ap_mode', 'play_audio', 'scan_networks')
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
    data = request.json or {}
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    if not ssid: return jsonify({'error': 'SSID requerido'}), 400
    plush = sb_get('plushes', 'plush_token', f'owner_id=eq.{user["id"]}')
    if not plush: return jsonify({'error': 'Sin peluche vinculado'}), 404
    with pending_commands_lock:
        pending_commands[plush['plush_token']] = {'action': 'wifi_change', 'ssid': ssid, 'password': password}
    return jsonify({'status': 'queued'})

# Networks scan (stored per plush_token in memory)
available_networks = {}

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
    token = plush['plush_token']
    nets = available_networks.get(token, [])
    status = 'ready' if nets else 'waiting'
    return jsonify({'status': status, 'networks': nets})

@app.route('/networks/results', methods=['POST'])
def networks_results_post():
    token = request.headers.get('X-Plush-Token', '')
    if not token: return jsonify({'error': 'No token'}), 401
    data = request.json or {}
    nets = data.get('networks', [])
    available_networks[token] = nets
    return jsonify({'ok': True})

# ── Admin ─────────────────────────────────────────────────────────────

@app.route('/admin/users', methods=['GET'])
@require_admin
def admin_list_users(user):
    try:
        r = requests.get(f"{SUPABASE_URL}/auth/v1/admin/users",
                        headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
                        timeout=10)
        users = r.json().get('users', [])
        profiles = sb_get_all('profiles', '*')
        profile_map = {p['id']: p for p in profiles}
        result = []
        for u in users:
            p = profile_map.get(u['id'], {})
            result.append({'id': u['id'], 'email': u.get('email'), 'role': p.get('role','user'),
                           'name': p.get('name',''), 'created_at': u.get('created_at')})
        return jsonify(result)
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
    return jsonify({'active_sessions': len(session_data), 'sessions': stats,
                    'pending_commands': len(pending_commands)})

@app.route('/admin/plushes', methods=['GET'])
@require_admin
def admin_plushes(user):
    return jsonify(sb_get_all('plushes', '*'))

# ── Audio serving ─────────────────────────────────────────────────────

@app.route('/audio/<filename>')
def serve_audio(filename):
    path = AUDIO_DIR / filename
    if not path.exists():
        return jsonify({'error': 'Not found'}), 404
    return send_file(str(path), mimetype='audio/mpeg')

# ── AI Pipeline ───────────────────────────────────────────────────────

def extract_text(msg: dict) -> str:
    text = msg.get('content') or msg.get('reasoning') or ''
    if not text and msg.get('reasoning_details'):
        for rd in msg['reasoning_details']:
            if rd.get('text'): return rd['text']
    if not text:
        raw = str(msg)
        m = re.search(r'<think>(.*?)</think>', raw, re.DOTALL)
        if m: return m.group(1).strip()
    return text.strip()

def format_memory(summary: str, user_id: str, plush) -> list:
    if not summary or not summary.strip(): return []
    try:
        model = plush.get('model', 'arcee-ai/trinity-large-preview:free') if plush else 'arcee-ai/trinity-large-preview:free'
        r = requests.post('https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}', 'Content-Type': 'application/json'},
            json={'model': model, 'max_tokens': 400,
                  'messages': [{'role': 'user', 'content':
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
    r = requests.post('https://api.elevenlabs.io/v1/speech-to-text',
        headers={'xi-api-key': ELEVENLABS_API_KEY},
        files={'file': ('audio.wav', audio_data, 'audio/wav')},
        data={'model_id': 'scribe_v1', 'tag_audio_events': 'false',
              'diarize': 'false', 'language_code': language},
        timeout=30)
    try: return r.json().get('text', '').strip()
    except: return ''

def chat_with_memory(user_id: str, plush) -> str:
    sess = get_session(user_id)
    row = sb_get('memory', 'summary', f'user_id=eq.{user_id}') or {}
    summary = row.get('summary', '')

    persona = plush.get('persona', '') if plush else ''
    if not persona: persona = _default_persona
    system_content = persona
    if summary:
        # Nota: quité la doble diagonal \\n para que el salto de línea sea real
        system_content += f"\n\nRecuerdas esto del usuario:\n{summary}"

    model = plush.get('model', 'arcee-ai/trinity-large-preview:free') if plush else 'arcee-ai/trinity-large-preview:free'
    messages = [{'role': 'system', 'content': system_content}] + [
        {'role': m['role'], 'content': m['content']} for m in sess['history'][-10:]
    ]
    
    print(f"🧠 Llamando a OpenRouter con modelo: {model}...", flush=True)
    r = requests.post('https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}', 'Content-Type': 'application/json'},
        json={'model': model, 'max_tokens': 150, 'messages': messages}, timeout=30)
    
    data = r.json()
    if not data.get('choices'):
        # ¡AQUI ESTA LA MAGIA! Esto imprimirá por qué OpenRouter falló.
        print("\n🚨 ERROR DE OPENROUTER:", data, flush=True)
        return ''
        
    return extract_text(data['choices'][0]['message'])

def update_summary(user_id: str, history: list, plush):
    try:
        row = sb_get('memory', 'summary', f'user_id=eq.{user_id}')
        if not row:
            sb_upsert('memory', {'user_id': user_id, 'summary': '',
                                  'updated_at': datetime.now(timezone.utc).isoformat()})
        old = (row or {}).get('summary', '')
        conv = '\n'.join([f"{'Usuario' if m['role']=='user' else 'PlushMate'}: {m['content']}"
                         for m in history[-10:]])
        model = plush.get('model', 'arcee-ai/trinity-large-preview:free') if plush else 'arcee-ai/trinity-large-preview:free'
        r = requests.post('https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}', 'Content-Type': 'application/json'},
            json={'model': model, 'max_tokens': 200,
                  'messages': [{'role': 'user', 'content':
                    f'Resumen existente: {old}\nConversación nueva:\n{conv}\n'
                    f'Actualiza el resumen en 3-5 oraciones. Solo el resumen.'}]},
            timeout=15)
        msg = r.json()['choices'][0]['message']
        new_summary = extract_text(msg)
        if new_summary:
            sb_upsert('memory', {'user_id': user_id, 'summary': new_summary,
                                  'updated_at': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f"[summary] ERROR: {e}")

def tts(text: str, voice_id: str = '') -> str:
    vid = voice_id or os.environ.get('ELEVENLABS_VOICE_ID', 'aaf0KU31jmlzVPqltvJY')
    r = requests.post(f'https://api.elevenlabs.io/v1/text-to-speech/{vid}/stream',
        headers={'xi-api-key': ELEVENLABS_API_KEY, 'Content-Type': 'application/json'},
        json={'text': text, 'model_id': 'eleven_v3',
              'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}},
        stream=True, timeout=30)
    if r.status_code != 200:
        raise Exception(f"TTS error {r.status_code}: {r.text}")
    filename = f"{uuid.uuid4()}.mp3"
    path = AUDIO_DIR / filename
    with open(str(path), 'wb') as f:
        for chunk in r.iter_content(chunk_size=4096):
            if chunk: f.write(chunk)
    files = sorted(AUDIO_DIR.glob('*.mp3'), key=lambda p: p.stat().st_mtime)
    for old_file in files[:-50]:
        try: old_file.unlink()
        except: pass
    return filename

# ── Keep-alive ────────────────────────────────────────────────────────

def keep_alive():
    while True:
        time.sleep(300)
        try: requests.get(f"{SERVER_URL}/health", timeout=5)
        except: pass

threading.Thread(target=keep_alive, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
