# app.py — PlushMate AI Server v4.0
# Stack: Deepgram Nova-2 (STT) → OpenRouter (LLM) → ElevenLabs (TTS)
# Memoria: historial de sesión (RAM) + resumen persistente (Supabase)
# Control remoto: polling desde ESP32 + app web PWA

import requests, os, tempfile, uuid, threading, time, struct, json, hashlib, re
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

# STT now uses ElevenLabs Scribe (ELEVENLABS_API_KEY already configured above)
OPENROUTER_API_KEY  = os.environ.get('OPENROUTER_API_KEY', '').strip()
ELEVENLABS_API_KEY  = os.environ.get('ELEVENLABS_API_KEY', '').strip()
ELEVENLABS_VOICE_ID = os.environ.get('ELEVENLABS_VOICE_ID', 'aaf0KU31jmlzVPqltvJY').strip()
SUPABASE_URL        = os.environ.get('SUPABASE_URL', '').strip()
SUPABASE_KEY        = os.environ.get('SUPABASE_KEY', '').strip()
OPENROUTER_MODEL    = os.environ.get('OPENROUTER_MODEL', 'arcee-ai/trinity-large-preview:free').strip()
APP_SECRET          = os.environ.get('APP_SECRET', 'plushmate2024').strip()

_server_url = os.environ.get('SERVER_URL', 'http://localhost:5000').strip()
SERVER_URL = _server_url if _server_url.startswith('http') else 'https://' + _server_url

_default_persona = "Eres PlushMate, un peluche mágico con inteligencia artificial. Eres amable, divertido y cálido. Hablas siempre en español. Responde de forma corta y amigable, máximo 2 oraciones."
PERSONA = os.environ.get('PERSONA', _default_persona).strip()

AUDIO_DIR = '/tmp/plushmate_audio'
os.makedirs(AUDIO_DIR, exist_ok=True)
LAST_WAV_PATH = '/tmp/plushmate_audio/last_debug.wav'

# ── Estado en RAM ─────────────────────────────────────────────────────────────

conversation_history = []
interaction_count = 0
HISTORY_LIMIT = 20
SUMMARY_EVERY = 5

# Cola de comandos para el ESP32 (polling)
pending_command = None
pending_command_lock = threading.Lock()

# Config dinámica (se puede cambiar desde la app sin redeploy)
dynamic_config = {
    'persona': PERSONA,
    'voice_id': ELEVENLABS_VOICE_ID,
    'model': OPENROUTER_MODEL,
    'wifi_ssid': '',
    'wifi_password': '',
    'wifi_pending': False
}

# ── Auth helper ───────────────────────────────────────────────────────────────

def check_auth():
    secret = request.headers.get('X-App-Secret') or request.args.get('secret', '')
    return secret == APP_SECRET

# ── Supabase ──────────────────────────────────────────────────────────────────

def supabase_get_summary() -> str:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return ""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memory?id=eq.1&select=summary",
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
            timeout=5
        )
        data = r.json()
        if data and len(data) > 0:
            return data[0].get('summary', '')
    except Exception as e:
        print(f"[Memory] Error leyendo Supabase: {e}")
    return ""

def supabase_save_summary(summary: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/memory?id=eq.1",
            headers={
                'apikey': SUPABASE_KEY,
                'Authorization': f'Bearer {SUPABASE_KEY}',
                'Content-Type': 'application/json',
                'Prefer': 'return=minimal'
            },
            json={'summary': summary},
            timeout=5
        )
        print(f"[Memory] Resumen guardado")
    except Exception as e:
        print(f"[Memory] Error guardando: {e}")

def update_summary_if_needed():
    global interaction_count
    if interaction_count % SUMMARY_EVERY != 0:
        return
    if len(conversation_history) < 2:
        return
    current_summary = supabase_get_summary()
    recent = "\n".join([
        f"{'Usuario' if m['role']=='user' else 'PlushMate'}: {m['content']}"
        for m in conversation_history[-10:]
    ])
    prompt = f"""Basándote en el resumen anterior y la conversación reciente, genera un resumen actualizado y conciso de lo que sabes sobre el dueño de PlushMate (nombre, edad, intereses, datos importantes). Solo hechos confirmados. Máximo 5 oraciones.

Resumen anterior:
{current_summary if current_summary else '(ninguno aún)'}

Conversación reciente:
{recent}

Nuevo resumen:"""
    try:
        r = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}', 'Content-Type': 'application/json', 'X-Title': 'PlushMate'},
            json={'model': dynamic_config['model'], 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 200},
            timeout=20
        )
        data = r.json()
        if data.get('choices'):
            msg = data['choices'][0]['message']
            new_summary = (msg.get('content') or msg.get('reasoning') or '').strip()
            supabase_save_summary(new_summary)
            print(f"[Memory] Resumen actualizado: {new_summary}")
    except Exception as e:
        print(f"[Memory] Error actualizando: {e}")

# ── Endpoints principales ─────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_file('static/index.html')

@app.route('/manifest.json')
def manifest():
    return send_file('static/manifest.json', mimetype='application/manifest+json')

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return send_file('static/assetlinks.json', mimetype='application/json')

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '4.0', 'history_len': len(conversation_history)})

@app.route('/process', methods=['POST'])
def process_audio():
    global conversation_history, interaction_count
    wav_bytes = request.data
    if not wav_bytes:
        return jsonify({'error': 'No audio received'}), 400
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(wav_bytes)
            tmp_path = f.name
        print(f"[PlushMate] WAV recibido: {len(wav_bytes)} bytes")
        with open(LAST_WAV_PATH, 'wb') as dbg:
            dbg.write(wav_bytes)
        transcript = stt(tmp_path)
        print(f"[PlushMate] Transcripción: {transcript}")
        if not transcript:
            return jsonify({'error': 'No speech detected'}), 400
        conversation_history.append({'role': 'user', 'content': transcript, 'audio_url': None})
        ai_text = chat_with_memory()
        print(f"[PlushMate] Respuesta IA: {ai_text}")
        # Strip ElevenLabs tags for display (keep original for TTS)
        display_text = re.sub(r'\[.*?\]', '', ai_text).strip()
        conversation_history.append({'role': 'assistant', 'content': display_text, 'audio_url': None})
        if len(conversation_history) > HISTORY_LIMIT:
            conversation_history = conversation_history[-HISTORY_LIMIT:]
        interaction_count += 1
        threading.Thread(target=update_summary_if_needed, daemon=True).start()
        audio_filename = tts(ai_text)
        audio_url = f"{SERVER_URL}/audio/{audio_filename}"
        # Attach audio_url to last assistant message
        conversation_history[-1]['audio_url'] = audio_url
        return jsonify({'transcript': transcript, 'response': display_text, 'audio_url': audio_url})
    except Exception as e:
        print(f"[PlushMate] ERROR: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

@app.route('/audio/<filename>')
def serve_audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Not found'}), 404
    return send_file(path, mimetype='audio/mpeg')

@app.route('/debug_audio')
def debug_audio():
    if not os.path.exists(LAST_WAV_PATH):
        return jsonify({'error': 'No hay audio guardado'}), 404
    return send_file(LAST_WAV_PATH, mimetype='audio/wav', as_attachment=True, download_name='debug.wav')

# ── Endpoints de memoria ──────────────────────────────────────────────────────

@app.route('/memory', methods=['GET'])
def get_memory():
    return jsonify({
        'summary': supabase_get_summary(),
        'history': conversation_history,
        'history_len': len(conversation_history)
    })

@app.route('/memory', methods=['DELETE'])
def clear_memory():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    # PIN verification required
    body = request.json or {}
    pin = body.get('pin', '')
    if pin:
        row = sb_get('pin')
        stored_hash = row.get('hash', '') if row else ''
        if stored_hash and pin_hash(pin) != stored_hash:
            return jsonify({'error': 'PIN incorrecto'}), 401
    global conversation_history, interaction_count
    conversation_history = []
    interaction_count = 0
    supabase_save_summary('')
    return jsonify({'status': 'memory cleared'})

# ── Endpoints de configuración (desde la app) ─────────────────────────────────

@app.route('/config', methods=['GET'])
def get_config():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'persona': dynamic_config['persona'],
        'voice_id': dynamic_config['voice_id'],
        'model': dynamic_config['model'],
    })

@app.route('/config', methods=['POST'])
def set_config():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    if 'persona' in data:
        dynamic_config['persona'] = data['persona']
    if 'voice_id' in data:
        dynamic_config['voice_id'] = data['voice_id']
    if 'model' in data:
        dynamic_config['model'] = data['model']
    return jsonify({'status': 'ok', 'config': dynamic_config})

# ── Endpoints de control del ESP32 (polling) ──────────────────────────────────

@app.route('/command', methods=['GET'])
def get_command():
    """ESP32 llama esto cada 3s. Si hay comando pendiente, lo devuelve y lo borra."""
    global pending_command
    with pending_command_lock:
        cmd = pending_command
        pending_command = None
    if cmd:
        print(f"[Command] ESP32 recogió comando: {cmd}")
        return jsonify(cmd)
    return jsonify({'action': 'none'})

@app.route('/command', methods=['POST'])
def send_command():
    """La app envía un comando para el ESP32."""
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    global pending_command
    data = request.json or {}
    action = data.get('action', '')
    if action not in ('activate', 'stop', 'wifi_change', 'volume_set'):
        return jsonify({'error': 'Unknown action'}), 400
    with pending_command_lock:
        pending_command = data
    print(f"[Command] Comando encolado: {data}")
    return jsonify({'status': 'queued', 'command': data})

@app.route('/wifi', methods=['POST'])
def set_wifi():
    """La app envía nuevas credenciales WiFi para el ESP32."""
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    if not ssid:
        return jsonify({'error': 'SSID requerido'}), 400
    global pending_command
    with pending_command_lock:
        pending_command = {'action': 'wifi_change', 'ssid': ssid, 'password': password}
    print(f"[WiFi] Cambio de red encolado: {ssid}")
    return jsonify({'status': 'queued'})

# ── Pipeline IA ───────────────────────────────────────────────────────────────

def stt(wav_path: str) -> str:
    """Speech-to-text usando ElevenLabs Scribe v1."""
    with open(wav_path, 'rb') as f:
        audio_data = f.read()
    r = requests.post(
        'https://api.elevenlabs.io/v1/speech-to-text',
        headers={'xi-api-key': ELEVENLABS_API_KEY},
        files={'file': ('audio.wav', audio_data, 'audio/wav')},
        data={
            'model_id': 'scribe_v1',
            'tag_audio_events': 'false',
            'diarize': 'false',
        },
        timeout=30
    )
    print(f"[STT] ElevenLabs status: {r.status_code}")
    data = r.json()
    print(f"[STT] ElevenLabs response: {data}")
    try:
        return data['text'].strip()
    except (KeyError, TypeError):
        return ''

def chat_with_memory() -> str:
    summary = supabase_get_summary()
    system_content = dynamic_config['persona']
    if summary:
        system_content += f"\n\nLo que recuerdas de tu dueño:\n{summary}"
    messages = [{'role': 'system', 'content': system_content}] + conversation_history
    r = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {OPENROUTER_API_KEY}', 'Content-Type': 'application/json', 'X-Title': 'PlushMate'},
        json={'model': dynamic_config['model'], 'messages': messages, 'max_tokens': 120},
        timeout=30
    )
    data = r.json()
    print(f"[Chat] response: {data}")
    if 'error' in data:
        return "Lo siento, no pude pensar en una respuesta ahora."
    if not data.get('choices'):
        return "Hmm, tuve un problema al procesar eso."
    msg = data['choices'][0]['message']
    # Some models (reasoning models) put response in 'reasoning' when content is None
    text = msg.get('content') or msg.get('reasoning') or ''
    if not text and msg.get('reasoning_details'):
        for rd in msg['reasoning_details']:
            if rd.get('text'):
                text = rd['text']
                break
    return text.strip()

def tts(text: str) -> str:
    r = requests.post(
        f'https://api.elevenlabs.io/v1/text-to-speech/{dynamic_config["voice_id"]}',
        headers={'xi-api-key': ELEVENLABS_API_KEY, 'Content-Type': 'application/json'},
        json={'text': text, 'model_id': 'eleven_v3', 'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}},
        timeout=30
    )
    if r.status_code != 200:
        print(f"[TTS] error {r.status_code}: {r.text}")
        raise Exception(f"ElevenLabs error: {r.status_code}")
    name = f"{uuid.uuid4()}.mp3"
    with open(os.path.join(AUDIO_DIR, name), 'wb') as f:
        f.write(r.content)
    print(f"[TTS] OK → {name}")
    return name


# ── Auth / PIN ────────────────────────────────────────────────────────────────

def pin_hash(pin):
    return hashlib.sha256(pin.strip().encode()).hexdigest()

def sb_get(table, select='*', filter_str='id=eq.1'):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{filter_str}&select={select}",
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
            timeout=5
        )
        data = r.json()
        return data[0] if data else None
    except:
        return None

def sb_patch(table, body, filter_str='id=eq.1'):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?{filter_str}",
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}',
                     'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
            json=body, timeout=5
        )
        return True
    except:
        return False

def sb_upsert(table, body):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}',
                     'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates'},
            json=body, timeout=5
        )
        return True
    except:
        return False

@app.route('/auth/status')
def auth_status():
    row = sb_get('pin')
    has_pin = bool(row and row.get('hash'))
    return jsonify({'has_pin': has_pin})

@app.route('/auth/verify', methods=['POST'])
def auth_verify():
    data = request.json or {}
    pin = data.get('pin', '')
    device_id = data.get('device_id', '')
    device_name = data.get('device_name', 'Dispositivo')
    if not pin or len(pin) != 4 or not pin.isdigit():
        return jsonify({'error': 'PIN invalido'}), 400
    row = sb_get('pin')
    stored_hash = row.get('hash', '') if row else ''
    if not stored_hash:
        return jsonify({'error': 'No hay PIN configurado'}), 400
    if pin_hash(pin) != stored_hash:
        return jsonify({'error': 'PIN incorrecto'}), 401
    if device_id:
        sb_upsert('devices', {'id': device_id, 'name': device_name, 'last_seen': 'now()', 'revoked': False})
    return jsonify({'ok': True})

@app.route('/auth/setup', methods=['POST'])
def auth_setup():
    data = request.json or {}
    new_pin = data.get('new_pin', '')
    current_pin = data.get('current_pin', '')
    if not new_pin or len(new_pin) != 4 or not new_pin.isdigit():
        return jsonify({'error': 'PIN invalido'}), 400
    row = sb_get('pin')
    stored_hash = row.get('hash', '') if row else ''
    if stored_hash:
        if not current_pin or pin_hash(current_pin) != stored_hash:
            return jsonify({'error': 'PIN actual incorrecto'}), 401
    sb_patch('pin', {'hash': pin_hash(new_pin)})
    return jsonify({'ok': True})

@app.route('/auth/checkin', methods=['POST'])
def auth_checkin():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    device_id = data.get('device_id', '')
    device_name = data.get('device_name', 'Dispositivo')
    if not device_id:
        return jsonify({'revoked': False})
    row = sb_get('devices', filter_str=f'id=eq.{device_id}')
    if row and row.get('revoked'):
        return jsonify({'revoked': True})
    sb_upsert('devices', {'id': device_id, 'name': device_name, 'last_seen': 'now()', 'revoked': False})
    return jsonify({'revoked': False})

# ── Profile ───────────────────────────────────────────────────────────────────

@app.route('/profile', methods=['GET'])
def get_profile():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    row = sb_get('profile') or {}
    return jsonify({'name': row.get('name',''), 'avatar': row.get('avatar','🐻'), 'color': row.get('color','#8FA0CA')})

@app.route('/profile', methods=['POST'])
def set_profile():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    body = {}
    if 'name' in data:   body['name']   = data['name']
    if 'avatar' in data: body['avatar'] = data['avatar']
    if 'color' in data:  body['color']  = data['color']
    sb_patch('profile', body)
    return jsonify({'ok': True})

# ── Devices ───────────────────────────────────────────────────────────────────

@app.route('/devices', methods=['GET'])
def list_devices():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify([])
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/devices?select=*&order=last_seen.desc",
            headers={'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'},
            timeout=5
        )
        return jsonify(r.json())
    except:
        return jsonify([])

@app.route('/devices/<device_id>', methods=['DELETE'])
def revoke_device(device_id):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    sb_patch('devices', {'revoked': True}, filter_str=f'id=eq.{device_id}')
    return jsonify({'ok': True})


# ── Keep-alive ────────────────────────────────────────────────────────────────

def keep_alive():
    while True:
        time.sleep(600)
        try:
            requests.get(f'{SERVER_URL}/health', timeout=5)
            print("[Keep-alive] ping OK")
        except Exception as e:
            print(f"[Keep-alive] fallo: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    threading.Thread(target=keep_alive, daemon=True).start()
    print(f"[Boot] Resumen actual: {supabase_get_summary() or '(vacío)'}")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
