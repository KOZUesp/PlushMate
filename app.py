# app.py — PlushMate AI Server
# Stack: Deepgram Nova-2 (STT) → OpenRouter (LLM) → ElevenLabs (TTS)

from flask import Flask, request, jsonify, send_file
import requests, os, tempfile, uuid, threading, time, struct

app = Flask(__name__)

DEEPGRAM_API_KEY   = os.environ.get('DEEPGRAM_API_KEY', '').strip()
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '').strip()
ELEVENLABS_API_KEY  = os.environ.get('ELEVENLABS_API_KEY', '').strip()
ELEVENLABS_VOICE_ID = os.environ.get('ELEVENLABS_VOICE_ID', 'aaf0KU31jmlzVPqltvJY').strip()
_server_url = os.environ.get('SERVER_URL', 'http://localhost:5000').strip()
SERVER_URL = _server_url if _server_url.startswith('http') else 'https://' + _server_url

_default_persona = "Eres PlushMate, un peluche mágico con inteligencia artificial. Eres amable, divertido y cálido. Hablas siempre en español. Responde de forma corta y amigable, máximo 2 oraciones."
PERSONA = os.environ.get('PERSONA', _default_persona).strip()

AUDIO_DIR = '/tmp/plushmate_audio'
os.makedirs(AUDIO_DIR, exist_ok=True)

# Guarda el último WAV recibido para debug
LAST_WAV_PATH = '/tmp/plushmate_audio/last_debug.wav'


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'name': 'PlushMate AI Server', 'status': 'online', 'version': '2.0'})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'PlushMate AI'})

@app.route('/process', methods=['POST'])
def process_audio():
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

        # 1. STT con Deepgram Nova-2
        transcript = stt(tmp_path)
        print(f"[PlushMate] Transcripción: {transcript}")
        if not transcript:
            return jsonify({'error': 'No speech detected'}), 400

        # 2. Respuesta con OpenRouter
        ai_text = chat(transcript)
        print(f"[PlushMate] Respuesta IA: {ai_text}")

        # 3. TTS con ElevenLabs
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
    """Descarga el último WAV recibido para verificar que el audio es correcto."""
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
        transcript = data['results']['channels'][0]['alternatives'][0]['transcript']
        return transcript.strip()
    except (KeyError, IndexError) as e:
        print(f"[STT] Error parseando respuesta: {e}")
        return ''


def chat(text: str) -> str:
    """Genera respuesta usando OpenRouter."""
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json',
        'X-Title': 'PlushMate'
    }
    body = {
        'model': 'arcee-ai/trinity-large-preview:free',
        'messages': [
            {'role': 'system', 'content': PERSONA},
            {'role': 'user',   'content': text}
        ],
        'max_tokens': 120
    }
    r = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers=headers,
        json=body,
        timeout=30
    )
    data = r.json()
    print(f"[Chat] OpenRouter response: {data}")

    if 'error' in data:
        print(f"[Chat] ERROR de OpenRouter: {data['error']}")
        return "Lo siento, no pude pensar en una respuesta ahora."

    if not data.get('choices'):
        print(f"[Chat] Respuesta inesperada (sin choices): {data}")
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
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
