# app.py — PlushMate AI Server
# Stack: Groq Whisper (STT) → OpenRouter (LLM) → ElevenLabs (TTS)

from flask import Flask, request, jsonify, send_file
import requests, os, tempfile, uuid, threading, time, struct

app = Flask(__name__)

GROQ_API_KEY       = os.environ.get('GROQ_API_KEY', '')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY', '')
ELEVENLABS_VOICE_ID = os.environ.get('ELEVENLABS_VOICE_ID', 'EXAVITQu4vr4xnSDxMaL')
SERVER_URL         = os.environ.get('SERVER_URL', 'http://localhost:5000')

PERSONA = """Eres PlushMate, un peluche mágico con inteligencia artificial.
Eres amable, divertido y cálido. Hablas siempre en español.
Responde de forma corta y amigable, máximo 2 oraciones."""

AUDIO_DIR = '/tmp/plushmate_audio'
os.makedirs(AUDIO_DIR, exist_ok=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'name': 'PlushMate AI Server', 'status': 'online', 'version': '1.0'})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'PlushMate AI'})

@app.route('/process', methods=['POST'])
def process_audio():
    wav_bytes = request.data
    if not wav_bytes:
        return jsonify({'error': 'No audio received'}), 400

    # Guardar WAV temporal (el ESP32 ya envía el header WAV completo)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(wav_bytes)
            tmp_path = f.name

        print(f"[PlushMate] WAV recibido: {len(wav_bytes)} bytes")

        # 1. STT con Groq Whisper
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


@app.route('/audio/<filename>')
def serve_audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Not found'}), 404
    return send_file(path, mimetype='audio/mpeg')


# ── Pipeline IA ───────────────────────────────────────────────────────────────

def stt(wav_path: str) -> str:
    """Transcribe WAV usando Groq Whisper."""
    with open(wav_path, 'rb') as f:
        r = requests.post(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}'},
            files={'file': ('audio.wav', f, 'audio/wav')},
            data={'model': 'whisper-large-v3-turbo', 'language': 'es'},
            timeout=30
        )
    data = r.json()
    print(f"[STT] Groq response: {data}")
    return data.get('text', '').strip()


def chat(text: str) -> str:
    """Genera respuesta usando OpenRouter (modelo gratuito)."""
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json',
        'X-Title': 'PlushMate'
    }
    body = {
        # FIX: modelo actualizado y confiable en tier gratuito
        'model': 'meta-llama/llama-3.1-8b-instruct:free',
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

    # FIX: manejo de error — antes crasheaba con KeyError: 'choices'
    if 'error' in data:
        print(f"[Chat] ERROR de OpenRouter: {data['error']}")
        return "Lo siento, no pude pensar en una respuesta ahora."

    if not data.get('choices'):
        print(f"[Chat] Respuesta inesperada (sin choices): {data}")
        return "Hmm, tuve un problema al procesar eso."

    return data['choices'][0]['message']['content'].strip()


def tts(text: str) -> str:
    """Genera MP3 con ElevenLabs y lo guarda en AUDIO_DIR."""
    r = requests.post(
        f'https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}',
        headers={
            'xi-api-key': ELEVENLABS_API_KEY,
            'Content-Type': 'application/json'
        },
        json={
            'text': text,
            'model_id': 'eleven_turbo_v2_5',
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
    return name


# ── Keep-alive (evita que Render duerma) ─────────────────────────────────────

def keep_alive():
    while True:
        time.sleep(600)  # ping cada 10 minutos
        try:
            requests.get(f'{SERVER_URL}/health', timeout=5)
            print("[Keep-alive] ping OK")
        except Exception as e:
            print(f"[Keep-alive] fallo: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    threading.Thread(target=keep_alive, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
