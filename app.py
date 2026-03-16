# app.py — PlushMate AI Server
# Stack: Deepgram Nova-2 (STT) → OpenRouter (LLM) → ElevenLabs (TTS)
# Memoria: historial de sesión (RAM) + resumen persistente (Supabase)

import requests, os, tempfile, uuid, threading, time, struct

app_module = __import__('flask')
Flask = app_module.Flask
request_obj = app_module.request
jsonify = app_module.jsonify
send_file = app_module.send_file

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DEEPGRAM_API_KEY    = os.environ.get('DEEPGRAM_API_KEY', '').strip()
OPENROUTER_API_KEY  = os.environ.get('OPENROUTER_API_KEY', '').strip()
ELEVENLABS_API_KEY  = os.environ.get('ELEVENLABS_API_KEY', '').strip()
ELEVENLABS_VOICE_ID = os.environ.get('ELEVENLABS_VOICE_ID', 'aaf0KU31jmlzVPqltvJY').strip()
SUPABASE_URL        = os.environ.get('SUPABASE_URL', '').strip()
OPENROUTER_MODEL    = os.environ.get('OPENROUTER_MODEL', 'arcee-ai/trinity-large-preview:free').strip()
SUPABASE_KEY        = os.environ.get('SUPABASE_KEY', '').strip()

_server_url = os.environ.get('SERVER_URL', 'http://localhost:5000').strip()
SERVER_URL = _server_url if _server_url.startswith('http') else 'https://' + _server_url

_default_persona = "Eres PlushMate, un peluche mágico con inteligencia artificial. Eres amable, divertido y cálido. Hablas siempre en español. Responde de forma corta y amigable, máximo 2 oraciones."
PERSONA = os.environ.get('PERSONA', _default_persona).strip()

AUDIO_DIR = '/tmp/plushmate_audio'
os.makedirs(AUDIO_DIR, exist_ok=True)

LAST_WAV_PATH = '/tmp/plushmate_audio/last_debug.wav'

# ── Memoria ───────────────────────────────────────────────────────────────────

# Historial de sesión en RAM (se borra al reiniciar el servidor)
conversation_history = []
interaction_count = 0
HISTORY_LIMIT = 20       # máximo de mensajes a mantener
SUMMARY_EVERY = 5        # actualizar resumen cada N interacciones

def supabase_get_summary() -> str:
    """Lee el resumen persistente desde Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return ""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memory?id=eq.1&select=summary",
            headers={
                'apikey': SUPABASE_KEY,
                'Authorization': f'Bearer {SUPABASE_KEY}'
            },
            timeout=5
        )
        data = r.json()
        if data and len(data) > 0:
            return data[0].get('summary', '')
    except Exception as e:
        print(f"[Memory] Error leyendo Supabase: {e}")
    return ""

def supabase_save_summary(summary: str):
    """Guarda el resumen persistente en Supabase."""
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
        print(f"[Memory] Resumen guardado en Supabase")
    except Exception as e:
        print(f"[Memory] Error guardando Supabase: {e}")

def update_summary_if_needed():
    """Cada SUMMARY_EVERY interacciones, pide al LLM que actualice el resumen."""
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

    prompt = f"""Basándote en el resumen anterior y la conversación reciente, genera un resumen actualizado y conciso de lo que sabes sobre el dueño de PlushMate (nombre, edad, intereses, datos importantes, etc). Solo incluye hechos relevantes y confirmados. Máximo 5 oraciones.

Resumen anterior:
{current_summary if current_summary else '(ninguno aún)'}

Conversación reciente:
{recent}

Nuevo resumen:"""

    try:
        r = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
                'X-Title': 'PlushMate'
            },
            json={
                'model': OPENROUTER_MODEL,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': 200
            },
            timeout=20
        )
        data = r.json()
        if data.get('choices'):
            new_summary = data['choices'][0]['message']['content'].strip()
            supabase_save_summary(new_summary)
            print(f"[Memory] Resumen actualizado: {new_summary}")
    except Exception as e:
        print(f"[Memory] Error actualizando resumen: {e}")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'name': 'PlushMate AI Server', 'status': 'online', 'version': '3.0'})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'PlushMate AI', 'history_len': len(conversation_history)})

@app.route('/memory', methods=['GET'])
def get_memory():
    """Ver el resumen actual guardado."""
    return jsonify({'summary': supabase_get_summary(), 'history_len': len(conversation_history)})

@app.route('/memory', methods=['DELETE'])
def clear_memory():
    """Borrar historial de sesión y resumen persistente."""
    global conversation_history, interaction_count
    conversation_history = []
    interaction_count = 0
    supabase_save_summary('')
    return jsonify({'status': 'memory cleared'})

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

        # Guardar copia para debug
        with open(LAST_WAV_PATH, 'wb') as dbg:
            dbg.write(wav_bytes)

        # DEBUG WAV header
        if len(wav_bytes) >= 44:
            ch   = struct.unpack_from('<H', wav_bytes, 22)[0]
            sr   = struct.unpack_from('<I', wav_bytes, 24)[0]
            bits = struct.unpack_from('<H', wav_bytes, 34)[0]
            ds   = struct.unpack_from('<I', wav_bytes, 40)[0]
            print(f"[WAV] channels={ch} samplerate={sr} bits={bits} datasize={ds}")

        # 1. STT
        transcript = stt(tmp_path)
        print(f"[PlushMate] Transcripción: {transcript}")
        if not transcript:
            return jsonify({'error': 'No speech detected'}), 400

        # 2. Añadir al historial
        conversation_history.append({'role': 'user', 'content': transcript})

        # 3. LLM con memoria
        ai_text = chat_with_memory()
        print(f"[PlushMate] Respuesta IA: {ai_text}")

        # 4. Añadir respuesta al historial
        conversation_history.append({'role': 'assistant', 'content': ai_text})

        # 5. Recortar historial si excede el límite
        if len(conversation_history) > HISTORY_LIMIT:
            conversation_history = conversation_history[-HISTORY_LIMIT:]

        # 6. Actualizar resumen si toca
        interaction_count += 1
        threading.Thread(target=update_summary_if_needed, daemon=True).start()

        # 7. TTS
        audio_filename = tts(ai_text)
        audio_url = f"{SERVER_URL}/audio/{audio_filename}"

        return jsonify({
            'transcript': transcript,
            'response': ai_text,
            'audio_url': audio_url
        })

    except Exception as e:
        print(f"[PlushMate] ERROR en /process: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.route('/debug_audio')
def debug_audio():
    if not os.path.exists(LAST_WAV_PATH):
        return jsonify({'error': 'No hay audio guardado aún'}), 404
    return send_file(LAST_WAV_PATH, mimetype='audio/wav', as_attachment=True, download_name='debug.wav')

@app.route('/audio/<filename>')
def serve_audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Not found'}), 404
    return send_file(path, mimetype='audio/mpeg')


# ── Pipeline IA ───────────────────────────────────────────────────────────────

def stt(wav_path: str) -> str:
    """Transcribe WAV usando Deepgram Nova-2."""
    with open(wav_path, 'rb') as f:
        audio_data = f.read()
    r = requests.post(
        'https://api.deepgram.com/v1/listen?model=nova-2&detect_language=true&smart_format=true&no_delay=true',
        headers={
            'Authorization': f'Token {DEEPGRAM_API_KEY}',
            'Content-Type': 'audio/wav'
        },
        data=audio_data,
        timeout=30
    )
    print(f"[STT] Deepgram status: {r.status_code}")
    data = r.json()
    print(f"[STT] Deepgram response: {data}")
    try:
        return data['results']['channels'][0]['alternatives'][0]['transcript'].strip()
    except (KeyError, IndexError) as e:
        print(f"[STT] Error parseando respuesta: {e}")
        return ''


def chat_with_memory() -> str:
    """Genera respuesta usando OpenRouter con historial + resumen persistente."""
    # Construir system prompt con resumen si existe
    summary = supabase_get_summary()
    system_content = PERSONA
    if summary:
        system_content += f"\n\nLo que recuerdas de tu dueño:\n{summary}"

    messages = [{'role': 'system', 'content': system_content}] + conversation_history

    r = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {OPENROUTER_API_KEY}',
            'Content-Type': 'application/json',
            'X-Title': 'PlushMate'
        },
        json={
            'model': OPENROUTER_MODEL,
            'messages': messages,
            'max_tokens': 120
        },
        timeout=30
    )
    data = r.json()
    print(f"[Chat] OpenRouter response: {data}")

    if 'error' in data:
        print(f"[Chat] ERROR de OpenRouter: {data['error']}")
        return "Lo siento, no pude pensar en una respuesta ahora."
    if not data.get('choices'):
        return "Hmm, tuve un problema al procesar eso."

    return data['choices'][0]['message']['content'].strip()


def tts(text: str) -> str:
    """Genera MP3 con ElevenLabs."""
    r = requests.post(
        f'https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}',
        headers={
            'xi-api-key': ELEVENLABS_API_KEY,
            'Content-Type': 'application/json'
        },
        json={
            'text': text,
            'model_id': 'eleven_v3',
            'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}
        },
        timeout=30
    )
    if r.status_code != 200:
        print(f"[TTS] ElevenLabs error {r.status_code}: {r.text}")
        raise Exception(f"ElevenLabs error: {r.status_code}")
    name = f"{uuid.uuid4()}.mp3"
    with open(os.path.join(AUDIO_DIR, name), 'wb') as f:
        f.write(r.content)
    print(f"[TTS] ElevenLabs OK → {name}")
    return name


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
