# app.py
from flask import Flask, request, jsonify, send_file
import requests, os, tempfile, uuid, threading, time, struct

app = Flask(__name__)

GROQ_API_KEY        = os.environ.get('GROQ_API_KEY', '')
OPENROUTER_API_KEY  = os.environ.get('OPENROUTER_API_KEY', '')
ELEVENLABS_API_KEY  = os.environ.get('ELEVENLABS_API_KEY', '')
ELEVENLABS_VOICE_ID = os.environ.get('ELEVENLABS_VOICE_ID', 'EXAVITQu4vr4xnSDxMaL')
SERVER_URL          = os.environ.get('SERVER_URL', 'http://localhost:5000')

PERSONA = """Eres PlushMate, un peluche mágico con inteligencia artificial.
Eres amable, divertido y cálido. Hablas siempre en español.
Responde de forma corta y amigable, máximo 2 oraciones."""

AUDIO_DIR = '/tmp/plushmate_audio'
os.makedirs(AUDIO_DIR, exist_ok=True)

def build_wav(pcm: bytes, rate=16000, ch=1, bits=16) -> bytes:
    byte_rate = rate * ch * bits // 8
    block_align = ch * bits // 8
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + len(pcm), b'WAVE',
        b'fmt ', 16, 1, ch, rate,
        byte_rate, block_align, bits,
        b'data', len(pcm))
    return header + pcm

# ── Endpoints ────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'PlushMate AI'})

@app.route('/process', methods=['POST'])
def process_audio():
    pcm = request.data
    if not pcm:
        return jsonify({'error': 'No audio received'}), 400

    # Guardar WAV temporal
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        f.write(build_wav(pcm))
        tmp = f.name

    try:
        transcript   = stt(tmp)
        if not transcript:
            return jsonify({'error': 'No speech detected'}), 400
        ai_text      = chat(transcript)
        audio_file   = tts(ai_text)
        audio_url    = f"{SERVER_URL}/audio/{audio_file}"

        return jsonify({
            'transcript': transcript,
            'response':   ai_text,
            'audio_url':  audio_url
        })
    finally:
        os.unlink(tmp)

@app.route('/audio/<filename>')
def serve_audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Not found'}), 404
    return send_file(path, mimetype='audio/mpeg')

# ── IA pipeline ──────────────────────────────────────────────────

def stt(wav_path: str) -> str:
    with open(wav_path, 'rb') as f:
        r = requests.post(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {GROQ_API_KEY}'},
            files={'file': ('audio.wav', f, 'audio/wav')},
            data={'model': 'whisper-large-v3-turbo', 'language': 'es'}
        )
    return r.json().get('text', '').strip()

def chat(transcript):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "meta-llama/llama-4-scout",   # ← verifica que sea exacto
        "messages": [{"role": "user", "content": transcript}]
    }
    r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                      headers=headers, json=body)
    
    data = r.json()
    
    # Imprime la respuesta completa para debug
    print(f"OpenRouter response: {data}")
    
    if 'error' in data:
        print(f"ERROR OpenRouter: {data['error']}")
        return "Lo siento, no pude procesar eso."
    
    return data['choices'][0]['message']['content']

def tts(text: str) -> str:
    r = requests.post(
        f'https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}',
        headers={'xi-api-key': ELEVENLABS_API_KEY, 'Content-Type': 'application/json'},
        json={
            'text': text,
            'model_id': 'eleven_turbo_v2_5',
            'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}
        }
    )
    name = f"{uuid.uuid4()}.mp3"
    with open(os.path.join(AUDIO_DIR, name), 'wb') as f:
        f.write(r.content)
    return name

# ── Keep-alive (evita que Render duerma) ────────────────────────
def keep_alive():
    while True:
        time.sleep(600)
        try: requests.get(f'{SERVER_URL}/health', timeout=5)
        except: pass

if __name__ == '__main__':
    threading.Thread(target=keep_alive, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

