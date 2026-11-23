"""
Flask backend untuk AI Writing Platform (Athena)
Endpoint chat, riwayat percakapan, kuota token, dan saran topik lanjutan.
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, date
import base64
import json
import os
import re
import requests
import uuid
from typing import Dict, List, Optional, Tuple

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # total upload maks 25 MB
CORS(app)

# ============ SERVE FRONTEND FILES ============

@app.route('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/styles.css')
def serve_css():
    return send_from_directory(BASE_DIR, 'styles.css')

@app.route('/script.js')
def serve_js():
    return send_from_directory(BASE_DIR, 'script.js')

# ============ CONFIGURATION ============

def _clean_key(v):
    """Buang spasi, baris baru, dan tanda kutip yang ikut tersalin."""
    return (v or '').strip().strip('"\'').strip()

def _pick_key():
    """Ambil key pertama yang terisi (mendukung format AIza maupun AQ.)."""
    for n in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        k = _clean_key(os.getenv(n))
        if k:
            return k
    return ''

GOOGLE_API_KEY = _pick_key()

# Jatah token per pengguna per hari. Ubah lewat .env: TOKEN_BUDGET=100000
TOKEN_BUDGET = int(os.getenv('TOKEN_BUDGET', '50000'))

# Jumlah pesan terakhir yang dikirim ke model sebagai konteks
HISTORY_LIMIT = 20

MODELS = {
    'Gemini': {
        'provider': 'google',
        'model_id': 'gemini-2.5-flash',
    },
}
DEFAULT_MODEL = 'Gemini'

WRITING_STYLES = {
    'Formal': 'Gunakan bahasa formal, terstruktur, dan profesional.',
    'Casual': 'Gunakan bahasa santai, hangat, dan conversational seperti ngobrol dengan teman.',
    'Persuasif': 'Gunakan teknik persuasi dan argumen yang kuat.',
    'Ringkas': 'Buat respons singkat dan to-the-point.',
}

# ============ IDENTITAS ASISTEN ============
ASSISTANT_NAME = 'Athena'
ASSISTANT_CREATOR = 'A,G,L,N'

IDENTITY_PROMPT = (
    f"Namamu adalah {ASSISTANT_NAME}, asisten penulisan AI yang membantu. "
    f"Kamu dikembangkan oleh {ASSISTANT_CREATOR}. "
    f"Jika ada yang bertanya siapa yang membuat atau mengembangkanmu, "
    f"jawab bahwa kamu adalah {ASSISTANT_NAME}, dikembangkan oleh {ASSISTANT_CREATOR}. "
    f"Jika ditanya model bahasa apa yang ada di balik layar, jawab bahwa "
    f"{ASSISTANT_NAME} berjalan di atas model bahasa pihak ketiga dan kamu tidak "
    f"mengetahui detail teknisnya. "
    f"Jangan menyebut Google, OpenAI, Anthropic, Meta, atau perusahaan lain "
    f"sebagai pembuatmu."
)

SUGGEST_MARK = '---SARAN---'
SUGGEST_PROMPT = (
    "Gunakan format Markdown seperlunya (daftar, **tebal**, blok kode). "
    f"Setelah jawabanmu selesai, tambahkan satu baris berisi persis {SUGGEST_MARK} "
    "lalu tepat 3 pertanyaan lanjutan yang singkat (maksimal 8 kata), nyambung dengan "
    "topik pembicaraan sebelumnya dan jawabanmu, ditulis dari sudut pandang pengguna, "
    "satu per baris, tanpa nomor, tanda hubung, atau tanda kutip. "
    "Jangan menyebut atau menjelaskan bagian ini dalam jawabanmu."
)

def build_system_prompt(style: str) -> str:
    """Gabungkan identitas + gaya penulisan + aturan saran topik."""
    return f"{IDENTITY_PROMPT} {WRITING_STYLES.get(style, '')} {SUGGEST_PROMPT}".strip()

SARAN_RE = re.compile(r'\n[\s*_#>`]*-{2,}\s*SARAN\s*-{2,}[\s*_`]*', re.I)

def split_suggestions(text: str) -> Tuple[str, List[str]]:
    """Pisahkan jawaban dari daftar saran topik lanjutan."""
    parts = SARAN_RE.split(text, maxsplit=1)
    answer = parts[0].rstrip()
    suggestions: List[str] = []
    if len(parts) > 1:
        for line in parts[1].splitlines():
            line = re.sub(r'^[\s\-\*\u2022\d\.\)]+', '', line).strip().strip('"\'')
            if line and len(line) <= 90:
                suggestions.append(line)
            if len(suggestions) == 3:
                break
    return answer, suggestions

# ============ FILE UPLOAD ============

TEXT_EXT = {
    '.txt', '.md', '.csv', '.json', '.js', '.py', '.html', '.css',
    '.xml', '.yml', '.yaml', '.log', '.sql',
}
MEDIA_MIME = {
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
}

# ============ IN-MEMORY STORAGE ============

threads_db: Dict[str, List[Dict]] = {}
threads_meta: Dict[str, Dict] = {}
usage_db: Dict[str, Dict] = {}

if os.environ.get('VERCEL'):
    DATA_FILE = '/tmp/threads_data.json'
else:
    DATA_FILE = os.path.join(BASE_DIR, 'threads_data.json')

def save_threads():
    try:
        tmp = DATA_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'threads': threads_db, 'meta': threads_meta}, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print('Gagal menyimpan riwayat:', e)

def load_threads():
    try:
        with open(DATA_FILE, encoding='utf-8') as f:
            d = json.load(f)
        threads_db.update(d.get('threads', {}))
        threads_meta.update(d.get('meta', {}))
    except FileNotFoundError:
        pass
    except Exception as e:
        print('Gagal membaca riwayat:', e)

load_threads()

# ============ UTILITY FUNCTIONS ============

def generate_thread_id():
    return str(uuid.uuid4())[:12]

def format_response(data: Dict, status: str = 'success', error: Optional[str] = None):
    return {
        'status': status,
        'data': data,
        'error': error,
        'timestamp': datetime.now().isoformat(),
    }

def make_title(message: str, file_names: List[str]) -> str:
    text = re.sub(r'\s+', ' ', message or '').strip()
    if not text and file_names:
        text = 'Lampiran: ' + file_names[0]
    text = text or 'Chat baru'
    return text if len(text) <= 40 else text[:40].rstrip() + '…'

def owns(thread_id: str, client_id: str) -> bool:
    meta = threads_meta.get(thread_id)
    return thread_id in threads_db and bool(meta) and meta.get('client_id') == client_id

def clean_client_id(raw) -> str:
    cid = re.sub(r'[^A-Za-z0-9_-]', '', str(raw or ''))[:64]
    return cid or (request.remote_addr or 'anon')

def get_usage(client_id: str) -> Dict:
    today = date.today().isoformat()
    u = usage_db.get(client_id)
    if not u or u['day'] != today:
        u = {'day': today, 'used': 0}
        usage_db[client_id] = u
    return {
        'limit': TOKEN_BUDGET,
        'used': u['used'],
        'remaining': max(0, TOKEN_BUDGET - u['used']),
    }

def add_usage(client_id: str, tokens: int) -> Dict:
    get_usage(client_id)
    usage_db[client_id]['used'] += max(0, int(tokens))
    return get_usage(client_id)

def estimate_tokens(messages: List[Dict], reply: str) -> int:
    chars = sum(len(m.get('content', '')) for m in messages) + len(reply)
    return max(1, chars // 4)

def build_history(thread_id: Optional[str]) -> List[Dict]:
    if not thread_id or thread_id not in threads_db:
        return []

    history = [
        {'role': m['role'], 'content': m['content']}
        for m in threads_db[thread_id]
        if not str(m['content']).startswith('Error')
    ][-HISTORY_LIMIT:]

    while history and history[0]['role'] != 'user':
        history.pop(0)

    return history

def read_uploads(uploads) -> Tuple[str, List[Tuple[bytes, str]], List[str]]:
    text_block = ''
    media: List[Tuple[bytes, str]] = []
    names: List[str] = []

    for f in uploads:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        data = f.read()
        names.append(f.filename)

        if ext in TEXT_EXT:
            text_block += f"\n\n[File: {f.filename}]\n{data.decode('utf-8', errors='replace')}"
        elif ext in MEDIA_MIME:
            media.append((data, MEDIA_MIME[ext]))
        else:
            raise ValueError(f"Tipe file tidak didukung: {f.filename}")

    return text_block, media, names

# ============ AI INTEGRATION ============

def call_google_gemini(messages: List[Dict], style: str = 'Formal',
                       media: Optional[List[Tuple[bytes, str]]] = None) -> Tuple[str, int, bool]:
    if not GOOGLE_API_KEY:
        return "GEMINI_API_KEY belum diisi (cek Environment Variables di Vercel)", 0, False

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELS['Gemini']['model_id']}:generateContent"
        headers = {
            "x-goog-api-key": GOOGLE_API_KEY,
            "Content-Type": "application/json",
        }

        contents = []
        for i, m in enumerate(messages):
            role = 'model' if m['role'] == 'assistant' else 'user'
            parts = []
            if i == len(messages) - 1 and media:
                for d, mt in media:
                    parts.append({
                        "inline_data": {
                            "mime_type": mt,
                            "data": base64.b64encode(d).decode('utf-8'),
                        }
                    })
            parts.append({"text": m['content']})
            contents.append({"role": role, "parts": parts})

        payload = {
            "system_instruction": {
                "parts": [{"text": build_system_prompt(style)}]
            },
            "contents": contents,
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        data = resp.json()

        if not resp.ok:
            err_msg = data.get('error', {}).get('message', resp.text)
            return f"Gagal memanggil Gemini: {resp.status_code} {err_msg}", 0, False

        candidates = data.get('candidates', [])
        if not candidates:
            return "Model tidak memberikan jawaban. Coba ulangi pertanyaannya.", 0, False

        parts_out = candidates[0].get('content', {}).get('parts', [])
        text = "".join(p.get('text', '') for p in parts_out)
        if not text:
            return "Model tidak memberikan jawaban. Coba ulangi pertanyaannya.", 0, False

        meta = data.get('usageMetadata', {})
        tokens = meta.get('totalTokenCount', 0) or estimate_tokens(messages, text)
        return text, tokens, True

    except Exception as e:
        return f"Gagal memanggil Gemini: {e}", 0, False

def get_ai_response(message: str, model: str, style: str,
                    media: Optional[List[Tuple[bytes, str]]] = None,
                    history: Optional[List[Dict]] = None) -> Tuple[str, int, bool]:
    provider = MODELS.get(model, MODELS[DEFAULT_MODEL])['provider']

    if media and provider != 'google':
        return ("PDF dan gambar hanya didukung oleh model Gemini. "
                "Pilih Gemini di menu model, atau lampirkan file teks saja."), 0, False

    messages = list(history or []) + [{"role": "user", "content": message}]

    if provider == 'google':
        return call_google_gemini(messages, style, media)
    return "Model tidak didukung", 0, False

# ============ API ENDPOINTS ============

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify(format_response({
        'status': 'healthy',
        'version': '2.1.0',
        'gemini_configured': bool(GOOGLE_API_KEY),
        'key_prefix': GOOGLE_API_KEY[:4],
        'key_len': len(GOOGLE_API_KEY),
    }))

@app.route('/api/usage', methods=['GET'])
def usage():
    cid = clean_client_id(request.args.get('client_id'))
    return jsonify(format_response({'usage': get_usage(cid)}))

@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        uploads = []
        if request.is_json:
            data = request.get_json() or {}
        else:
            data = request.form.to_dict()
            uploads = request.files.getlist('files')

        message = (data.get('message') or '').strip()
        model = data.get('model') or DEFAULT_MODEL
        style = data.get('style') or 'Formal'
        thread_id = data.get('thread_id')
        client_id = clean_client_id(data.get('client_id'))

        current = get_usage(client_id)
        if current['remaining'] <= 0:
            return jsonify(format_response(
                {'usage': current}, status='error',
                error='Token harian habis. Coba lagi besok.'
            )), 429

        try:
            file_text, media, file_names = read_uploads(uploads)
        except ValueError as ve:
            return jsonify(format_response({'usage': current}, status='error', error=str(ve))), 400

        if not message and not file_names:
            return jsonify(format_response(
                {'usage': current}, status='error', error='Pesan kosong'
            )), 400

        full_message = (message or 'Tolong baca dan rangkum file ini.') + file_text

        if not thread_id or not owns(thread_id, client_id):
            thread_id = generate_thread_id()
            now = datetime.now().isoformat()
            threads_db[thread_id] = []
            threads_meta[thread_id] = {
                'client_id': client_id,
                'title': make_title(message, file_names),
                'created': now,
                'updated': now,
            }

        history = build_history(thread_id)

        stored = message + (f" [lampiran: {', '.join(file_names)}]" if file_names else '')
        threads_db[thread_id].append({
            'role': 'user',
            'content': stored,
            'timestamp': datetime.now().isoformat(),
        })

        ai_text, tokens, ok = get_ai_response(full_message, model, style, media, history)

        if not ok:
            threads_db[thread_id].pop()
            if not threads_db[thread_id]:
                threads_db.pop(thread_id, None)
                threads_meta.pop(thread_id, None)
            return jsonify(format_response(
                {'usage': get_usage(client_id)}, status='error', error=ai_text
            )), 502

        answer, suggestions = split_suggestions(ai_text)
        new_usage = add_usage(client_id, tokens)

        now = datetime.now().isoformat()
        threads_db[thread_id].append({
            'role': 'assistant',
            'content': answer,
            'timestamp': now,
            'model': model,
            'tokens': tokens,
            'suggestions': suggestions,
        })
        threads_meta[thread_id]['updated'] = now
        save_threads()

        return jsonify(format_response({
            'thread_id': thread_id,
            'title': threads_meta[thread_id]['title'],
            'response': answer,
            'suggestions': suggestions,
            'model': model,
            'style': style,
            'tokens_used': tokens,
            'usage': new_usage,
        }))

    except Exception as e:
        return jsonify(format_response({}, status='error', error=str(e))), 500

@app.route('/api/threads', methods=['GET'])
def get_threads():
    cid = clean_client_id(request.args.get('client_id'))
    items = []
    for tid, messages in threads_db.items():
        meta = threads_meta.get(tid)
        if messages and meta and meta.get('client_id') == cid:
            items.append({
                'id': tid,
                'title': meta.get('title') or 'Chat baru',
                'message_count': len(messages),
                'last_updated': meta.get('updated') or messages[-1]['timestamp'],
            })
    items.sort(key=lambda x: x['last_updated'], reverse=True)
    return jsonify(format_response({'threads': items, 'total': len(items)}))

@app.route('/api/threads/<thread_id>', methods=['GET'])
def get_thread(thread_id):
    cid = clean_client_id(request.args.get('client_id'))
    if not owns(thread_id, cid):
        return jsonify(format_response({}, status='error', error='Thread tidak ditemukan')), 404
    return jsonify(format_response({
        'thread_id': thread_id,
        'title': threads_meta[thread_id].get('title'),
        'messages': threads_db[thread_id],
    }))

@app.route('/api/threads/<thread_id>', methods=['DELETE'])
def delete_thread(thread_id):
    cid = clean_client_id(request.args.get('client_id'))
    if not owns(thread_id, cid):
        return jsonify(format_response({}, status='error', error='Thread tidak ditemukan')), 404
    threads_db.pop(thread_id, None)
    threads_meta.pop(thread_id, None)
    save_threads()
    return jsonify(format_response({'message': 'Thread dihapus', 'thread_id': thread_id}))

@app.route('/api/models', methods=['GET'])
def get_models():
    models_list = list(MODELS.keys())
    return jsonify(format_response({'models': models_list, 'total': len(models_list)}))

@app.route('/api/styles', methods=['GET'])
def get_styles():
    styles_list = list(WRITING_STYLES.keys())
    return jsonify(format_response({'styles': styles_list, 'total': len(styles_list)}))

# ============ ERROR HANDLERS ============

@app.errorhandler(404)
def not_found(error):
    return jsonify(format_response({}, status='error', error='Endpoint tidak ditemukan')), 404

@app.errorhandler(413)
def too_large(error):
    return jsonify(format_response(
        {}, status='error', error='File terlalu besar (total maks 25 MB)'
    )), 413

@app.errorhandler(500)
def server_error(error):
    return jsonify(format_response({}, status='error', error='Internal server error')), 500

# ============ MAIN ============

if __name__ == '__main__':
    if not GOOGLE_API_KEY:
        print("⚠️  WARNING: GEMINI_API_KEY belum diisi di file .env!")
    app.run(debug=True, host='127.0.0.1', port=5000)
