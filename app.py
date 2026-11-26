"""
Flask backend untuk AI Writing Platform (Athena)
Endpoint chat, autentikasi email (Supabase), riwayat percakapan, dan kuota token.
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, date, timezone
import json
import os
import re
import requests
import uuid
from typing import Dict, List, Optional, Tuple

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
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
    return (v or '').strip().strip('"\'').strip()

def _pick_key():
    for n in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        k = _clean_key(os.getenv(n))
        if k:
            return k
    return ''

def _pick_groq_key():
    return _clean_key(os.getenv('GROQ_API_KEY'))

GOOGLE_API_KEY = _pick_key()
GROQ_API_KEY = _pick_groq_key()

SUPABASE_URL = _clean_key(os.getenv('SUPABASE_URL')).rstrip('/')
SUPABASE_ANON_KEY = _clean_key(os.getenv('SUPABASE_ANON_KEY'))
SUPABASE_SERVICE_KEY = _clean_key(os.getenv('SUPABASE_SERVICE_ROLE_KEY')) or SUPABASE_ANON_KEY

TOKEN_BUDGET = int(os.getenv('TOKEN_BUDGET', '50000'))
HISTORY_LIMIT = 3

MODELS = {
    'Gemini': {
        'provider': 'google',
        'model_id': 'gemini-3.5-flash-lite',
    },
}
DEFAULT_MODEL = 'Gemini'

GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
]

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
    return f"{IDENTITY_PROMPT} {WRITING_STYLES.get(style, '')} {SUGGEST_PROMPT}".strip()

SARAN_RE = re.compile(r'\n[\s*_#>`]*-{2,}\s*SARAN\s*-{2,}[\s*_`]*', re.I)
THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.I)

def split_suggestions(text: str) -> Tuple[str, List[str]]:
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

# ============ STORAGE & SUPABASE HELPERS ============

threads_db: Dict[str, List[Dict]] = {}
threads_meta: Dict[str, Dict] = {}
usage_db: Dict[str, Dict] = {}

DATA_FILE = '/tmp/threads_data.json' if os.environ.get('VERCEL') else os.path.join(BASE_DIR, 'threads_data.json')

def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)

def _sb_headers(use_service: bool = True) -> Dict[str, str]:
    key = SUPABASE_SERVICE_KEY if use_service else SUPABASE_ANON_KEY
    return {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }

def get_authenticated_user() -> Optional[Dict]:
    """Verifikasi Bearer token dari header Authorization ke Supabase Auth."""
    if not supabase_enabled():
        return None
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth.split(' ', 1)[1].strip()
    if not token:
        return None
    try:
        res = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={'apikey': SUPABASE_ANON_KEY, 'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        if res.ok:
            u = res.json()
            meta = u.get('user_metadata') or {}
            email = u.get('email') or ''
            name = meta.get('full_name') or (email.split('@')[0] if email else 'User')
            return {'id': u['id'], 'email': email, 'name': name}
    except Exception:
        pass
    return None

def save_threads_local():
    try:
        tmp = DATA_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'threads': threads_db, 'meta': threads_meta}, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print('Gagal menyimpan riwayat lokal:', e)

def load_threads_local():
    try:
        with open(DATA_FILE, encoding='utf-8') as f:
            d = json.load(f)
        threads_db.update(d.get('threads', {}))
        threads_meta.update(d.get('meta', {}))
    except FileNotFoundError:
        pass
    except Exception as e:
        print('Gagal membaca riwayat lokal:', e)

load_threads_local()

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

def clean_client_id(raw) -> str:
    cid = re.sub(r'[^A-Za-z0-9_-]', '', str(raw or ''))[:64]
    return cid or (request.remote_addr or 'anon')

def resolve_owner(raw_cid) -> Tuple[str, Optional[Dict]]:
    """Kembalikan (owner_id, user_dict_jika_login)."""
    user = get_authenticated_user()
    if user:
        return user['id'], user
    return clean_client_id(raw_cid), None

# ---- Usage (Token Harian) ----

def get_usage(owner_id: str, is_user: bool = False) -> Dict:
    today = date.today().isoformat()
    if is_user and supabase_enabled():
        try:
            url = f"{SUPABASE_URL}/rest/v1/daily_usage?user_id=eq.{owner_id}&day=eq.{today}&select=used"
            res = requests.get(url, headers=_sb_headers(True), timeout=10)
            if res.ok and res.json():
                used = int(res.json()[0].get('used', 0))
                return {'limit': TOKEN_BUDGET, 'used': used, 'remaining': max(0, TOKEN_BUDGET - used)}
            return {'limit': TOKEN_BUDGET, 'used': 0, 'remaining': TOKEN_BUDGET}
        except Exception as e:
            print('Supabase usage read error:', e)

    u = usage_db.get(owner_id)
    if not u or u['day'] != today:
        u = {'day': today, 'used': 0}
        usage_db[owner_id] = u
    return {
        'limit': TOKEN_BUDGET,
        'used': u['used'],
        'remaining': max(0, TOKEN_BUDGET - u['used']),
    }

def add_usage(owner_id: str, tokens: int, is_user: bool = False) -> Dict:
    current = get_usage(owner_id, is_user)
    new_used = current['used'] + max(0, int(tokens))
    today = date.today().isoformat()

    if is_user and supabase_enabled():
        try:
            url = f"{SUPABASE_URL}/rest/v1/daily_usage"
            headers = {**_sb_headers(True), 'Prefer': 'resolution=merge-duplicates'}
            requests.post(
                url,
                headers=headers,
                json={'user_id': owner_id, 'day': today, 'used': new_used},
                timeout=10,
            )
            return {'limit': TOKEN_BUDGET, 'used': new_used, 'remaining': max(0, TOKEN_BUDGET - new_used)}
        except Exception as e:
            print('Supabase usage write error:', e)

    usage_db[owner_id] = {'day': today, 'used': new_used}
    return get_usage(owner_id, False)

# ---- Threads (Riwayat Chat) ----

def db_get_thread(thread_id: str, owner_id: str, is_user: bool) -> Optional[Dict]:
    if is_user and supabase_enabled():
        try:
            url = f"{SUPABASE_URL}/rest/v1/threads?id=eq.{thread_id}&user_id=eq.{owner_id}&select=*"
            res = requests.get(url, headers=_sb_headers(True), timeout=10)
            if res.ok and res.json():
                row = res.json()[0]
                return {'id': row['id'], 'title': row['title'], 'messages': row.get('messages') or []}
        except Exception as e:
            print('Supabase get thread error:', e)
        return None

    meta = threads_meta.get(thread_id)
    if thread_id in threads_db and meta and meta.get('client_id') == owner_id:
        return {'id': thread_id, 'title': meta.get('title'), 'messages': threads_db[thread_id]}
    return None

def db_save_thread(thread_id: str, owner_id: str, title: str, messages: List[Dict], is_user: bool):
    now = datetime.now(timezone.utc).isoformat()
    if is_user and supabase_enabled():
        try:
            url = f"{SUPABASE_URL}/rest/v1/threads"
            headers = {**_sb_headers(True), 'Prefer': 'resolution=merge-duplicates'}
            requests.post(
                url,
                headers=headers,
                json={
                    'id': thread_id,
                    'user_id': owner_id,
                    'title': title,
                    'messages': messages,
                    'updated_at': now,
                },
                timeout=10,
            )
            return
        except Exception as e:
            print('Supabase save thread error:', e)

    threads_db[thread_id] = messages
    if thread_id not in threads_meta:
        threads_meta[thread_id] = {'client_id': owner_id, 'title': title, 'created': now, 'updated': now}
    else:
        threads_meta[thread_id]['updated'] = now
    save_threads_local()

def estimate_tokens(messages: List[Dict], reply: str) -> int:
    chars = sum(len(m.get('content', '')) for m in messages) + len(reply)
    return max(1, chars // 4)

def build_history_from_list(raw_messages: List[Dict]) -> List[Dict]:
    history = [
        {'role': m['role'], 'content': m['content']}
        for m in raw_messages
        if not str(m.get('content', '')).startswith('Error')
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

# ============ AI INTEGRATION (GEMINI + GROQ FAILOVER) ============

def call_google_gemini(messages: List[Dict], style: str = 'Formal',
                       media: Optional[List[Tuple[bytes, str]]] = None) -> Tuple[str, int, bool]:
    api_key = _pick_key()
    if not api_key:
        return "GEMINI_API_KEY belum diisi", 0, False

    try:
        if api_key.startswith('AQ.'):
            convo = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in messages]
            url = "https://generativelanguage.googleapis.com/v1beta/interactions"
            headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
            payload = {
                "model": MODELS['Gemini']['model_id'],
                "input": "\n\n".join(convo),
                "system_instruction": build_system_prompt(style),
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            data = resp.json()
            if not resp.ok:
                err_msg = data.get('error', {}).get('message', resp.text)
                return f"Gagal memanggil Gemini: {resp.status_code} {err_msg}", 0, False

            text = ""
            for step in data.get('steps', []):
                if step.get('type') == 'model_output':
                    for item in step.get('content', []):
                        if item.get('type') == 'text':
                            text += item.get('text', '')
            if not text:
                text = "".join(out.get('text', '') for out in data.get('outputs', []) if out.get('type') == 'text')
            if not text:
                return "Model tidak memberikan jawaban teks.", 0, False

            tokens = data.get('usage', {}).get('total_tokens') or estimate_tokens(messages, text)
            return text, tokens, True

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        contents = []
        for i, m in enumerate(messages):
            role = 'model' if m['role'] == 'assistant' else 'user'
            parts = []
            if i == len(messages) - 1 and media:
                parts.extend(types.Part.from_bytes(data=d, mime_type=mt) for d, mt in media)
            parts.append(types.Part.from_text(text=m['content']))
            contents.append(types.Content(role=role, parts=parts))

        response = client.models.generate_content(
            model=MODELS['Gemini']['model_id'],
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=build_system_prompt(style)),
        )
        text = response.text or ''
        if not text:
            return "Model tidak memberikan jawaban.", 0, False

        meta = getattr(response, 'usage_metadata', None)
        tokens = getattr(meta, 'total_token_count', 0) or estimate_tokens(messages, text)
        return text, tokens, True

    except Exception as e:
        return f"Gagal memanggil Gemini: {e}", 0, False


def call_groq(model_id: str, messages: List[Dict], style: str = 'Formal') -> Tuple[str, int, bool]:
    api_key = _pick_groq_key()
    if not api_key:
        return "GROQ_API_KEY belum diisi", 0, False

    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        groq_messages = [{"role": "system", "content": build_system_prompt(style)}] + [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        resp = requests.post(
            url, headers=headers,
            json={"model": model_id, "messages": groq_messages, "temperature": 0.7},
            timeout=60,
        )
        data = resp.json()
        if not resp.ok:
            err_msg = data.get('error', {}).get('message', resp.text)
            return f"Gagal memanggil Groq ({model_id}): {resp.status_code} {err_msg}", 0, False

        choices = data.get('choices', [])
        if not choices:
            return f"Groq ({model_id}) tidak memberikan jawaban.", 0, False

        raw_text = choices[0].get('message', {}).get('content') or ''
        text = THINK_RE.sub('', raw_text).strip()
        if not text:
            return f"Groq ({model_id}) mengembalikan jawaban kosong.", 0, False

        tokens = data.get('usage', {}).get('total_tokens') or estimate_tokens(messages, text)
        return text, tokens, True

    except Exception as e:
        return f"Gagal memanggil Groq ({model_id}): {e}", 0, False


def get_ai_response(message: str, model: str, style: str,
                    media: Optional[List[Tuple[bytes, str]]] = None,
                    history: Optional[List[Dict]] = None) -> Tuple[str, int, bool]:
    messages = list(history or []) + [{"role": "user", "content": message}]

    text, tokens, ok = call_google_gemini(messages, style, media)
    if ok:
        return text, tokens, True

    if media:
        return f"{text} (Lampiran PDF/gambar tidak dapat dialihkan ke model cadangan)", 0, False

    if _pick_groq_key():
        last_err = text
        for fallback_id in GROQ_FALLBACK_MODELS:
            fb_text, fb_tokens, fb_ok = call_groq(fallback_id, messages, style)
            if fb_ok:
                return fb_text, fb_tokens, True
            last_err = fb_text
        return f"Gemini dan semua model cadangan gagal merespons. Error terakhir: {last_err}", 0, False

    return text, 0, False

# ============ AUTH ENDPOINTS ============

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    if not supabase_enabled():
        return jsonify(format_response({}, status='error', error='Supabase belum dikonfigurasi di server')), 500

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not name or not email or len(password) < 6:
        return jsonify(format_response(
            {}, status='error', error='Nama, email, dan password (minimal 6 karakter) wajib diisi.'
        )), 400

    try:
        res = requests.post(
            f"{SUPABASE_URL}/auth/v1/signup",
            headers=_sb_headers(False),
            json={'email': email, 'password': password, 'data': {'full_name': name}},
            timeout=15,
        )
        body = res.json()
        if not res.ok:
            msg = body.get('msg') or body.get('error_description') or body.get('message') or 'Gagal mendaftar'
            return jsonify(format_response({}, status='error', error=msg)), 400

        token = body.get('access_token')
        user = body.get('user') or {}
        if not token:
            return jsonify(format_response({
                'needs_verification': True,
                'message': 'Pendaftaran berhasil! Silakan cek email untuk verifikasi sebelum masuk.'
            }))

        return jsonify(format_response({
            'access_token': token,
            'user': {'id': user.get('id'), 'email': email, 'name': name},
        }))
    except Exception as e:
        return jsonify(format_response({}, status='error', error=str(e))), 500


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    if not supabase_enabled():
        return jsonify(format_response({}, status='error', error='Supabase belum dikonfigurasi di server')), 500

    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email or not password:
        return jsonify(format_response({}, status='error', error='Email dan password wajib diisi.')), 400

    try:
        res = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers=_sb_headers(False),
            json={'email': email, 'password': password},
            timeout=15,
        )
        body = res.json()
        if not res.ok:
            msg = body.get('error_description') or body.get('msg') or 'Email atau password salah.'
            return jsonify(format_response({}, status='error', error=msg)), 401

        user = body.get('user') or {}
        meta = user.get('user_metadata') or {}
        name = meta.get('full_name') or email.split('@')[0]

        return jsonify(format_response({
            'access_token': body.get('access_token'),
            'user': {'id': user.get('id'), 'email': user.get('email'), 'name': name},
        }))
    except Exception as e:
        return jsonify(format_response({}, status='error', error=str(e))), 500


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    user = get_authenticated_user()
    if not user:
        return jsonify(format_response({}, status='error', error='Sesi tidak valid atau sudah berakhir')), 401
    return jsonify(format_response({'user': user}))

# ============ API ENDPOINTS ============

@app.route('/api/health', methods=['GET'])
def health_check():
    current_key = _pick_key()
    return jsonify(format_response({
        'status': 'healthy',
        'version': '2.3.0',
        'gemini_configured': bool(current_key),
        'groq_configured': bool(_pick_groq_key()),
        'supabase_configured': supabase_enabled(),
        'model_id': MODELS['Gemini']['model_id'],
        'fallback_models': GROQ_FALLBACK_MODELS,
    }))

@app.route('/api/usage', methods=['GET'])
def usage():
    owner_id, user = resolve_owner(request.args.get('client_id'))
    return jsonify(format_response({'usage': get_usage(owner_id, bool(user))}))

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
        owner_id, user = resolve_owner(data.get('client_id'))
        is_user = bool(user)

        current = get_usage(owner_id, is_user)
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
            return jsonify(format_response({'usage': current}, status='error', error='Pesan kosong')), 400

        full_message = (message or 'Tolong baca dan rangkum file ini.') + file_text

        existing = db_get_thread(thread_id, owner_id, is_user) if thread_id else None
        if not existing:
            thread_id = generate_thread_id()
            title = make_title(message, file_names)
            msgs_list = []
        else:
            title = existing['title']
            msgs_list = list(existing['messages'])

        history = build_history_from_list(msgs_list)

        stored = message + (f" [lampiran: {', '.join(file_names)}]" if file_names else '')
        msgs_list.append({
            'role': 'user',
            'content': stored,
            'timestamp': datetime.now().isoformat(),
        })

        ai_text, tokens, ok = get_ai_response(full_message, model, style, media, history)

        if not ok:
            return jsonify(format_response(
                {'usage': get_usage(owner_id, is_user)}, status='error', error=ai_text
            )), 502

        answer, suggestions = split_suggestions(ai_text)
        new_usage = add_usage(owner_id, tokens, is_user)

        now = datetime.now().isoformat()
        msgs_list.append({
            'role': 'assistant',
            'content': answer,
            'timestamp': now,
            'model': model,
            'tokens': tokens,
            'suggestions': suggestions,
        })
        db_save_thread(thread_id, owner_id, title, msgs_list, is_user)

        return jsonify(format_response({
            'thread_id': thread_id,
            'title': title,
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
    owner_id, user = resolve_owner(request.args.get('client_id'))
    if user and supabase_enabled():
        try:
            url = (f"{SUPABASE_URL}/rest/v1/threads?user_id=eq.{owner_id}"
                   "&select=id,title,messages,updated_at&order=updated_at.desc")
            res = requests.get(url, headers=_sb_headers(True), timeout=10)
            if res.ok:
                items = [
                    {
                        'id': r['id'],
                        'title': r.get('title') or 'Chat baru',
                        'message_count': len(r.get('messages') or []),
                        'last_updated': r.get('updated_at'),
                    }
                    for r in res.json()
                ]
                return jsonify(format_response({'threads': items, 'total': len(items)}))
        except Exception as e:
            print('Supabase list threads error:', e)

    items = []
    for tid, messages in threads_db.items():
        meta = threads_meta.get(tid)
        if messages and meta and meta.get('client_id') == owner_id:
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
    owner_id, user = resolve_owner(request.args.get('client_id'))
    t = db_get_thread(thread_id, owner_id, bool(user))
    if not t:
        return jsonify(format_response({}, status='error', error='Thread tidak ditemukan')), 404
    return jsonify(format_response({
        'thread_id': t['id'],
        'title': t['title'],
        'messages': t['messages'],
    }))

@app.route('/api/threads/<thread_id>', methods=['DELETE'])
def delete_thread(thread_id):
    owner_id, user = resolve_owner(request.args.get('client_id'))
    if user and supabase_enabled():
        try:
            url = f"{SUPABASE_URL}/rest/v1/threads?id=eq.{thread_id}&user_id=eq.{owner_id}"
            requests.delete(url, headers=_sb_headers(True), timeout=10)
            return jsonify(format_response({'message': 'Thread dihapus', 'thread_id': thread_id}))
        except Exception as e:
            return jsonify(format_response({}, status='error', error=str(e))), 500

    meta = threads_meta.get(thread_id)
    if thread_id not in threads_db or not meta or meta.get('client_id') != owner_id:
        return jsonify(format_response({}, status='error', error='Thread tidak ditemukan')), 404
    threads_db.pop(thread_id, None)
    threads_meta.pop(thread_id, None)
    save_threads_local()
    return jsonify(format_response({'message': 'Thread dihapus', 'thread_id': thread_id}))

@app.route('/api/models', methods=['GET'])
def get_models():
    models_list = list(MODELS.keys())
    return jsonify(format_response({'models': models_list, 'total': len(models_list)}))

@app.route('/api/styles', methods=['GET'])
def get_styles():
    styles_list = list(WRITING_STYLES.keys())
    return jsonify(format_response({'styles': styles_list, 'total': len(styles_list)}))

@app.errorhandler(404)
def not_found(error):
    return jsonify(format_response({}, status='error', error='Endpoint tidak ditemukan')), 404

@app.errorhandler(413)
def too_large(error):
    return jsonify(format_response({}, status='error', error='File terlalu besar (total maks 25 MB)')), 413

@app.errorhandler(500)
def server_error(error):
    return jsonify(format_response({}, status='error', error='Internal server error')), 500

if __name__ == '__main__':
    if not GOOGLE_API_KEY:
        print("⚠️  WARNING: GEMINI_API_KEY belum diisi di file .env!")
    if not GROQ_API_KEY:
        print("⚠️  WARNING: GROQ_API_KEY belum diisi di file .env!")
    if not supabase_enabled():
        print("⚠️  WARNING: SUPABASE_URL / SUPABASE_ANON_KEY belum diisi di file .env!")
    app.run(debug=True, host='127.0.0.1', port=5000)
