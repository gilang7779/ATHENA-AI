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
HISTORY_LIMIT = 20
GUEST_MESSAGE_LIMIT = 3

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
                "model
