#!/usr/bin/env python3
"""Chill IELTS: small, self-contained teacher dashboard and task server.

No third-party packages are needed. It stores data in SQLite beside this file.
For public use, put it behind HTTPS (for example on Render, Railway, or a school server).
"""
from __future__ import annotations
import cgi, hashlib, hmac, json, os, secrets, sqlite3, time, shutil, uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib import request as urlrequest, error as urlerror

# ============================================================================
# PERSISTENT STORAGE CONFIGURATION
# ============================================================================
# Use Render's persistent disk if available, otherwise fall back to local data
PERSISTENT_DIR = Path(os.environ.get("PERSISTENT_DATA_DIR", "/data"))
if PERSISTENT_DIR.exists() and PERSISTENT_DIR.is_dir() and os.access(str(PERSISTENT_DIR), os.W_OK):
    DATA_BASE = PERSISTENT_DIR
    print(f"✅ Using persistent storage at: {DATA_BASE}")
else:
    DATA_BASE = Path(__file__).resolve().parent / "data"
    print(f"ℹ️ Using local storage at: {DATA_BASE}")

HERE = Path(__file__).resolve().parent
ROOT = HERE
DATA = DATA_BASE
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "chill_ielts.sqlite3"
CONFIG = DATA / "settings.json"

# Create directories if they don't exist
DATA.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)

# ============================================================================
# DATABASE SCHEMA & MIGRATION
# ============================================================================
CURRENT_SCHEMA_VERSION = 5

def get_db_version(db):
    """Get current database schema version"""
    try:
        result = db.execute("PRAGMA user_version").fetchone()
        return result[0] if result else 0
    except:
        return 0

def migrate_database(db):
    """Automatically migrate database to latest schema without data loss"""
    version = get_db_version(db)
    print(f"🔄 Current database version: {version}")
    
    if version < 1:
        print("📦 Creating initial schema...")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY, 
                title TEXT NOT NULL, 
                task1_title TEXT NOT NULL, 
                task1_prompt TEXT NOT NULL, 
                task1_image TEXT, 
                task2_title TEXT NOT NULL, 
                task2_prompt TEXT NOT NULL, 
                active INTEGER NOT NULL DEFAULT 1, 
                class_code TEXT UNIQUE,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                token_hash TEXT PRIMARY KEY, 
                test_id INTEGER NOT NULL, 
                student_name TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                attempt_uuid TEXT UNIQUE NOT NULL,
                FOREIGN KEY(test_id) REFERENCES tests(id)
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY, 
                test_id INTEGER NOT NULL, 
                student_name TEXT NOT NULL,
                attempt_token_hash TEXT NOT NULL,
                attempt_uuid TEXT NOT NULL,
                task1_answer TEXT, 
                task2_answer TEXT, 
                seconds_remaining INTEGER, 
                submitted_at INTEGER NOT NULL,
                FOREIGN KEY(test_id) REFERENCES tests(id)
            );
        """)
    
    if version < 2:
        print("📦 Adding autosave table...")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS autosave (
                id INTEGER PRIMARY KEY,
                attempt_token_hash TEXT NOT NULL UNIQUE,
                attempt_uuid TEXT NOT NULL,
                test_id INTEGER NOT NULL,
                student_name TEXT NOT NULL,
                task1_answer TEXT,
                task2_answer TEXT,
                saved_at INTEGER NOT NULL,
                FOREIGN KEY(test_id) REFERENCES tests(id)
            );
        """)
    
    if version < 3:
        print("📦 Adding AI feedback columns...")
        try:
            db.execute("ALTER TABLE submissions ADD COLUMN ai_feedback TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE submissions ADD COLUMN ai_score TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE submissions ADD COLUMN ai_marked_at INTEGER")
        except sqlite3.OperationalError:
            pass
        
        # Ensure attempt_token_hash column exists
        try:
            db.execute("ALTER TABLE submissions ADD COLUMN attempt_token_hash TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE submissions ADD COLUMN attempt_uuid TEXT")
        except sqlite3.OperationalError:
            pass
    
    if version < 4:
        print("📦 Adding completed column to attempts...")
        try:
            db.execute("ALTER TABLE attempts ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE attempts ADD COLUMN attempt_uuid TEXT")
        except sqlite3.OperationalError:
            pass
        
        # Drop old unique constraints if they exist (safe approach)
        try:
            indexes = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'sqlite_autoindex_submissions%'").fetchall()
            for idx in indexes:
                try:
                    db.execute(f"DROP INDEX IF EXISTS {idx['name']}")
                except:
                    pass
        except:
            pass
        
        # Create new unique index without dropping table
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_unique ON submissions(test_id, student_name, attempt_token_hash)")
        except:
            pass
        
        # Create index for fast lookups
        try:
            db.execute("CREATE INDEX IF NOT EXISTS idx_attempts_token_hash ON attempts(token_hash)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_attempts_test_student ON attempts(test_id, student_name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_attempts_uuid ON attempts(attempt_uuid)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_autosave_token_hash ON autosave(attempt_token_hash)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_autosave_uuid ON autosave(attempt_uuid)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_submissions_test_student ON submissions(test_id, student_name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_submissions_uuid ON submissions(attempt_uuid)")
        except:
            pass
    
    if version < 5:
        print("📦 Adding attempt_uuid column and ensuring uniqueness...")
        # Add attempt_uuid to attempts if missing
        try:
            db.execute("ALTER TABLE attempts ADD COLUMN attempt_uuid TEXT")
        except sqlite3.OperationalError:
            pass
        
        # Add attempt_uuid to submissions if missing
        try:
            db.execute("ALTER TABLE submissions ADD COLUMN attempt_uuid TEXT")
        except sqlite3.OperationalError:
            pass
        
        # Add attempt_uuid to autosave if missing
        try:
            db.execute("ALTER TABLE autosave ADD COLUMN attempt_uuid TEXT")
        except sqlite3.OperationalError:
            pass
        
        # Generate UUIDs for existing attempts
        existing_attempts = db.execute("SELECT token_hash FROM attempts WHERE attempt_uuid IS NULL OR attempt_uuid = ''").fetchall()
        for attempt in existing_attempts:
            new_uuid = str(uuid.uuid4())
            db.execute("UPDATE attempts SET attempt_uuid = ? WHERE token_hash = ?", (new_uuid, attempt['token_hash']))
            # Also update related submissions and autosave
            db.execute("UPDATE submissions SET attempt_uuid = ? WHERE attempt_token_hash = ?", (new_uuid, attempt['token_hash']))
            db.execute("UPDATE autosave SET attempt_uuid = ? WHERE attempt_token_hash = ?", (new_uuid, attempt['token_hash']))
        
        # Make attempt_uuid NOT NULL after populating
        try:
            # SQLite doesn't support ALTER COLUMN, so we need to handle this differently
            # We'll create a new table if needed, but for simplicity we'll just ensure the column exists
            pass
        except:
            pass
        
        # Create unique index on attempt_uuid
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_uuid_unique ON attempts(attempt_uuid)")
        except:
            pass
    
    # Set version to current
    db.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    print(f"✅ Database migrated to version {CURRENT_SCHEMA_VERSION}")

def conn():
    """Get database connection with automatic migration"""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    
    # Enable foreign keys
    db.execute("PRAGMA foreign_keys = ON")
    
    # Migrate database if needed
    migrate_database(db)
    
    return db

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================
def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

def generate_attempt_uuid() -> str:
    """Generate a unique attempt identifier"""
    return str(uuid.uuid4())

def load_settings():
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text())
        except:
            pass
    
    password = os.environ.get("ADMIN_PASSWORD")
    secret = os.environ.get("SESSION_SECRET")
    if not password:
        password = input("Create a teacher dashboard password: ").strip()
    if not password:
        raise SystemExit("A dashboard password is required.")
    
    settings = {
        "password_hash": sha(password),
        "secret": secret or secrets.token_urlsafe(32)
    }
    CONFIG.write_text(json.dumps(settings))
    os.chmod(CONFIG, 0o600)
    return settings

SETTINGS = load_settings()

def esc(value):
    import html
    return html.escape(str(value or ""), quote=True)

def page(title, body):
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>:root{{--navy:#123758;--orange:#ff5a16;--ink:#18344e;--muted:#617586}}*{{box-sizing:border-box}}body{{font:15px Arial;background:#f3f7fa;color:var(--ink);margin:0}}header{{background:#fff;padding:15px max(5%,22px);display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #d7e0e8;box-shadow:0 1px 5px #1231}}header b{{font-size:18px}}header a{{color:var(--navy);text-decoration:none;margin-left:12px;font-weight:bold}}main{{max-width:1120px;margin:32px auto;background:#fff;padding:30px;border-radius:10px;box-shadow:0 3px 20px #1232}}h1{{margin-top:0}}input,textarea,select{{width:100%;padding:11px;margin:5px 0 16px;border:1px solid #b8c7d3;border-radius:5px;font:inherit}}textarea{{min-height:110px}}button,.button{{background:var(--navy);color:#fff;border:0;padding:11px 16px;border-radius:5px;font-weight:bold;text-decoration:none;cursor:pointer;display:inline-block}}button:hover,.button:hover{{filter:brightness(1.1)}}.accent{{background:var(--orange)}}.danger{{background:#bd2d28}}.msg{{padding:12px;background:#e5f5ee;border-radius:5px}}.notice{{padding:12px;background:#fff4e9;border-left:4px solid var(--orange);border-radius:4px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #dde5eb;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:var(--muted);letter-spacing:.05em}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:20px 0}}.stat{{padding:18px;border:1px solid #d8e2ea;border-radius:8px;background:#fbfdfe}}.stat strong{{font-size:28px;display:block;color:var(--navy)}}pre{{white-space:pre-wrap;font:14px/1.55 Arial}}.score{{color:#087e54;font-weight:bold}}@media(max-width:650px){{.grid,.stats{{grid-template-columns:1fr}}header{{align-items:flex-start;gap:10px;flex-direction:column}}main{{margin:12px;padding:20px}}table{{font-size:13px}}}}</style></head><body><header><b>Chill IELTS · Teacher dashboard</b><span><a href='/admin'>Tests</a> <a href='/admin/submissions'>Submissions</a> <a href='/admin/logout'>Log out</a></span></header><main>{body}</main></body></html>"""

# ============================================================================
# MAIN SERVER
# ============================================================================
class App(BaseHTTPRequestHandler):
    server_version = "ChillIELTS/1.0"
    
    def log_message(self, fmt, *args):
        print(time.strftime("%H:%M:%S"), fmt % args)
    
    def send(self, status, body, content_type="text/html; charset=utf-8", headers=None):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)
    
    def json(self, status, value):
        self.send(status, json.dumps(value), "application/json; charset=utf-8", {"Cache-Control": "no-store"})
    
    def form(self):
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0).decode())
            except:
                return {}
        fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype})
        return {k: fs.getvalue(k) for k in fs.keys()}, fs
    
    def logged_in(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        token = cookie.get("teacher_session")
        if not token:
            return False
        try:
            val, sig = token.value.split(".", 1)
            expected = hmac.new(SETTINGS["secret"].encode(), val.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected) and int(val) > time.time()
        except Exception:
            return False
    
    def require_login(self):
        if self.logged_in():
            return True
        self.redirect('/admin/login')
        return False
    
    def redirect(self, where):
        self.send(HTTPStatus.SEE_OTHER, b"", headers={"Location": where})
    
    def static(self, path):
        """Serve static files from the correct location"""
        # Handle uploads - serve from DATA/uploads
        if path.startswith('/uploads/'):
            # Remove /uploads/ prefix and serve from UPLOADS directory
            file_path = path.replace('/uploads/', '', 1)
            target = (UPLOADS / file_path).resolve()
        else:
            # Serve from ROOT for other static files
            target = (ROOT / path.lstrip('/')).resolve()
        
        # Prevent path traversal
        if not str(target).startswith(str(ROOT.resolve())) and not str(target).startswith(str(UPLOADS.resolve())):
            print(f"⚠️ Path traversal attempt: {target}")
            return self.send(404, "Not found")
        
        if not target.is_file():
            print(f"❌ File not found: {target}")
            return self.send(404, "Not found")
        
        types = {
            '.html': 'text/html; charset=utf-8',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
            '.css': 'text/css',
            '.js': 'application/javascript'
        }
        
        self.send(200, target.read_bytes(), types.get(target.suffix.lower(), 'application/octet-stream'))
    
    # ========================================================================
    # GET HANDLERS
    # ========================================================================
    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        
        # API endpoints
        if p.path == '/api/test':
            return self.api_test(q.get('code', [''])[0], q.get('name', [''])[0])
        
        if p.path == '/api/autosave':
            return self.api_autosave_get(q.get('token', [''])[0])
        
        # Static file serving for uploads and other files
        if p.path.startswith('/uploads/'):
            return self.static(p.path)
        
        # Student page
        if p.path == '/' or p.path == '/practice':
            html_path = ROOT / 'ielts-writing-exam.html'
            if html_path.exists():
                return self.send(200, html_path.read_text(), 'text/html; charset=utf-8')
            return self.send(404, "File not found")
        
        if p.path == '/chill-ielts-logo.png':
            img_path = ROOT / 'chill-ielts-logo.png'
            if img_path.exists():
                return self.send(200, img_path.read_bytes(), 'image/png')
            return self.send(404, "File not found")
        
        # Admin routes
        if p.path == '/admin/login':
            return self.send(200, page('Teacher sign in',
                "<h1>Teacher sign in</h1><form method=post><label>Password</label><input name=password type=password autofocus required><button>Sign in</button></form>"
            ))
        
        if p.path == '/admin/logout':
            return self.send(303, b'', headers={
                'Location': '/admin/login',
                'Set-Cookie': 'teacher_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict'
            })
        
        if p.path == '/admin':
            return self.dashboard()
        
        if p.path == '/admin/new':
            return self.new_test()
        
        if p.path == '/admin/submissions':
            return self.submissions()
        
        # Static files
        static_extensions = {'.css', '.js', '.ico', '.txt', '.xml', '.json'}
        if Path(ROOT / p.path.lstrip('/')).exists() and Path(ROOT / p.path.lstrip('/')).suffix in static_extensions:
            return self.static(p.path)
        
        return self.send(404, "Not found")
    
    # ========================================================================
    # POST HANDLERS
    # ========================================================================
    def do_POST(self):
        p = urlparse(self.path)
        if p.path == '/api/submissions':
            return self.api_submission()
        if p.path == '/api/autosave':
            return self.api_autosave()
        if p.path == '/admin/login':
            return self.login()
        if p.path == '/admin/new':
            return self.create_test()
        if p.path.startswith('/admin/toggle/'):
            return self.toggle_test(p.path.rsplit('/', 1)[1])
        if p.path.startswith('/admin/grade/'):
            return self.grade_submission(p.path.rsplit('/', 1)[1])
        if p.path.startswith('/admin/generate-code/'):
            return self.generate_class_code(p.path.rsplit('/', 1)[1])
        return self.send(404, "Not found")
    
    # ========================================================================
    # API: TEST ACCESS - Creates new attempt with UUID
    # ========================================================================
    def api_test(self, raw_code, raw_name=''):
        code = raw_code.strip().upper()
        student_name = raw_name.strip()[:80]
        
        if not code:
            return self.json(400, {"error": "An access code is required."})
        if not student_name:
            return self.json(400, {"error": "Please enter your full name."})
        
        db = conn()
        
        # Find test by class code
        test = db.execute("SELECT * FROM tests WHERE class_code=? AND active=1", (code,)).fetchone()
        if not test:
            db.close()
            return self.json(404, {"error": "This access code is not valid or the test is inactive."})
        
        # Check for existing active attempt for THIS SPECIFIC student
        # CRITICAL: Must match student_name AND code AND not completed
        existing_attempt = db.execute("""
            SELECT token_hash, attempt_uuid FROM attempts 
            WHERE test_id=? AND student_name=? AND expires_at>? AND completed=0
        """, (test['id'], student_name, int(time.time()))).fetchone()
        
        # If there's an existing active attempt for this student, return it
        if existing_attempt:
            db.close()
            return self.json(200, {
                "title": test['title'],
                "attemptToken": existing_attempt['token_hash'],
                "attemptUuid": existing_attempt['attempt_uuid'],
                "task1": {"title": test['task1_title'], "prompt": test['task1_prompt'], "imageUrl": test['task1_image'] or ''},
                "task2": {"title": test['task2_title'], "prompt": test['task2_prompt']}
            })
        
        # Create new attempt with UUID
        token = secrets.token_urlsafe(32)
        token_hash = sha(token)
        attempt_uuid = generate_attempt_uuid()
        
        # Delete expired attempts
        db.execute("DELETE FROM attempts WHERE expires_at<?", (int(time.time()),))
        
        # Insert new attempt with UUID
        db.execute("""
            INSERT INTO attempts(token_hash, test_id, student_name, expires_at, completed, attempt_uuid) 
            VALUES (?,?,?,?,?,?)
        """, (token_hash, test['id'], student_name, int(time.time()) + 7200, 0, attempt_uuid))
        db.commit()
        db.close()
        
        return self.json(200, {
            "title": test['title'],
            "attemptToken": token,
            "attemptUuid": attempt_uuid,
            "task1": {"title": test['task1_title'], "prompt": test['task1_prompt'], "imageUrl": test['task1_image'] or ''},
            "task2": {"title": test['task2_title'], "prompt": test['task2_prompt']}
        })
    
    # ========================================================================
    # API: SUBMISSION - Uses attempt token, not student name
    # ========================================================================
    def api_submission(self):
        try:
            data = self.form()
            if isinstance(data, tuple):
                data = data[0]
        except Exception:
            return self.json(400, {"error": "Invalid submission."})
        
        raw_token = str(data.get('attemptToken', ''))
        if not raw_token:
            return self.json(400, {"error": "No attempt token provided."})
        
        token_hash = sha(raw_token)
        db = conn()
        
        # Verify attempt exists and is not completed - use token only
        attempt = db.execute("""
            SELECT * FROM attempts 
            WHERE token_hash=? AND expires_at>? AND completed=0
        """, (token_hash, int(time.time()))).fetchone()
        
        if not attempt:
            db.close()
            return self.json(403, {"error": "Your test session has expired or has already been submitted."})
        
        # Get student_name from the attempt
        student_name = attempt['student_name']
        test_id = attempt['test_id']
        attempt_uuid = attempt['attempt_uuid']
        
        # Create submission with attempt_uuid
        db.execute("""
            INSERT INTO submissions(
                test_id, student_name, attempt_token_hash, attempt_uuid,
                task1_answer, task2_answer, seconds_remaining, submitted_at
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (test_id, student_name, token_hash, attempt_uuid,
              str(data.get('task1Answer', '')), str(data.get('task2Answer', '')),
              int(data.get('secondsRemaining', 0) or 0), int(time.time())))
        
        # Mark attempt as completed
        db.execute("UPDATE attempts SET completed=1 WHERE token_hash=?", (token_hash,))
        
        # Clear autosave data for this attempt
        db.execute("DELETE FROM autosave WHERE attempt_token_hash=?", (token_hash,))
        
        db.commit()
        db.close()
        return self.json(200, {"ok": True})
    
    # ========================================================================
    # API: AUTOSAVE - Uses attempt token
    # ========================================================================
    def api_autosave(self):
        try:
            data = self.form()
            if isinstance(data, tuple):
                data = data[0]
        except Exception:
            return self.json(400, {"error": "Invalid autosave data."})
        
        raw_token = str(data.get('attemptToken', ''))
        task1 = str(data.get('task1Answer', ''))
        task2 = str(data.get('task2Answer', ''))
        
        if not raw_token:
            return self.json(400, {"error": "No session token."})
        
        token_hash = sha(raw_token)
        db = conn()
        
        # Verify attempt exists and is not completed - use token only
        attempt = db.execute("""
            SELECT * FROM attempts 
            WHERE token_hash=? AND expires_at>? AND completed=0
        """, (token_hash, int(time.time()))).fetchone()
        
        if not attempt:
            db.close()
            return self.json(403, {"error": "Your test session has expired or has already been submitted."})
        
        attempt_uuid = attempt['attempt_uuid']
        
        # Save or update autosave data with attempt_uuid
        db.execute("""
            INSERT INTO autosave(attempt_token_hash, attempt_uuid, test_id, student_name, task1_answer, task2_answer, saved_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(attempt_token_hash) DO UPDATE SET
                task1_answer=excluded.task1_answer,
                task2_answer=excluded.task2_answer,
                saved_at=excluded.saved_at
        """, (token_hash, attempt_uuid, attempt['test_id'], attempt['student_name'], task1, task2, int(time.time())))
        
        db.commit()
        db.close()
        return self.json(200, {"ok": True, "savedAt": int(time.time())})
    
    def api_autosave_get(self, raw_token):
        """Retrieve autosaved data - uses token only"""
        if not raw_token:
            return self.json(400, {"error": "No session token."})
        
        token_hash = sha(raw_token)
        db = conn()
        
        # Verify attempt exists and is not completed - use token only
        attempt = db.execute("""
            SELECT * FROM attempts 
            WHERE token_hash=? AND expires_at>? AND completed=0
        """, (token_hash, int(time.time()))).fetchone()
        
        if not attempt:
            db.close()
            return self.json(403, {"error": "Your test session has expired or has already been submitted."})
        
        # Get autosave data
        autosave = db.execute("""
            SELECT task1_answer, task2_answer 
            FROM autosave 
            WHERE attempt_token_hash=?
        """, (token_hash,)).fetchone()
        
        db.close()
        
        if autosave:
            return self.json(200, {
                "hasAutosave": True,
                "task1": autosave['task1_answer'] or '',
                "task2": autosave['task2_answer'] or ''
            })
        
        return self.json(200, {"hasAutosave": False})
    
    # ========================================================================
    # ADMIN: LOGIN
    # ========================================================================
    def login(self):
        data, _ = self.form()
        password = str(data.get('password', ''))
        
        if not hmac.compare_digest(sha(password), SETTINGS['password_hash']):
            return self.send(401, page('Teacher sign in',
                "<h1>Teacher sign in</h1><p class=msg>Incorrect password.</p><form method=post><input name=password type=password required><button>Sign in</button></form>"
            ))
        
        expiry = str(int(time.time()) + 28800)
        sig = hmac.new(SETTINGS['secret'].encode(), expiry.encode(), hashlib.sha256).hexdigest()
        self.send(303, b'', headers={
            'Location': '/admin',
            'Set-Cookie': f'teacher_session={expiry}.{sig}; Path=/; HttpOnly; SameSite=Strict'
        })
    
    # ========================================================================
    # ADMIN: DASHBOARD
    # ========================================================================
    def dashboard(self):
        if not self.require_login():
            return
        
        db = conn()
        tests = db.execute("SELECT * FROM tests ORDER BY id DESC").fetchall()
        submission_count = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        db.close()
        
        rows = ''.join(f"""
        <tr>
            <td><b>{esc(x['title'])}</b><br>
                <small>Code: <strong style='font-size:18px;letter-spacing:2px;'>{esc(x['class_code'])}</strong></small>
            </td>
            <td>{'✅ Active' if x['active'] else '⛔ Inactive'}</td>
            <td>
                <form method=post action='/admin/toggle/{x['id']}' style='display:inline;'>
                    <button class='{"danger" if x['active'] else ""}' style='padding:6px 12px;font-size:13px;'>
                        {x['active'] and 'Deactivate' or 'Activate'}
                    </button>
                </form>
            </td>
            <td>
                <form method=post action='/admin/generate-code/{x['id']}' style='display:inline;'>
                    <button style='padding:6px 12px;font-size:13px;'>🔄 New Code</button>
                </form>
            </td>
        </tr>
        """ for x in tests) or '<tr><td colspan=4>No tests created yet.</td></tr>'
        
        self.send(200, page('Tests', f"""
        <h1>📊 Your teaching workspace</h1>
        <p>Build tests, get a class code, and review completed work in one place.</p>
        <p>
            <a class='button accent' href='/admin/new'>+ Create a new test</a>
            <a class=button href='/practice'>Open student page</a>
            <a class=button href='/admin/submissions'>Review submissions</a>
        </p>
        <div class=stats>
            <div class=stat><strong>{len(tests)}</strong>Tests created</div>
            <div class=stat><strong>{submission_count}</strong>Submissions</div>
        </div>
        <table>
            <tr><th>Test</th><th>Status</th><th>Toggle</th><th>Class Code</th></tr>
            {rows}
        </table>
        """))
    
    # ========================================================================
    # ADMIN: CREATE TEST (with clipboard paste support)
    # ========================================================================
    def new_test(self):
        if not self.require_login():
            return
        self.send(200, page('New test', """
        <h1>📝 Create a test</h1>
        <form method=post enctype='multipart/form-data' id="testForm">
            <label>Test title</label>
            <input name=title required placeholder='Academic Writing Practice 1'>
            <div class=grid>
                <div>
                    <h2>Task 1</h2>
                    <label>Title</label>
                    <input name=task1_title value='Describe the information' required>
                    <label>Instructions / question</label>
                    <textarea name=task1_prompt required></textarea>
                    <label>Chart image (PNG, JPG or WebP)</label>
                    <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:5px 0 16px;">
                        <input type="file" name="chart" id="chartInput" accept="image/png,image/jpeg,image/webp" style="flex:1; min-width:200px; padding:8px;">
                        <button type="button" id="pasteBtn" class="button" style="background:var(--orange); padding:8px 16px; font-size:14px;">📋 Paste from Clipboard</button>
                    </div>
                    <div id="pasteStatus" style="font-size:13px; color:var(--good); margin: -10px 0 16px; display:none;"></div>
                    <div id="imagePreview" style="display:none; margin:10px 0;">
                        <img id="previewImg" style="max-width:100%; max-height:200px; border-radius:4px; border:1px solid var(--line);">
                        <button type="button" id="removeImgBtn" style="background:#bd2d28; color:#fff; border:0; padding:4px 12px; border-radius:4px; cursor:pointer; margin-top:5px; font-size:13px;">✖ Remove image</button>
                    </div>
                </div>
                <div>
                    <h2>Task 2</h2>
                    <label>Title</label>
                    <input name=task2_title value='Discuss both views and give your opinion' required>
                    <label>Instructions / question</label>
                    <textarea name=task2_prompt required></textarea>
                </div>
            </div>
            <button>Create test</button>
        </form>
        <script>
        (function() {
            'use strict';
            
            const chartInput = document.getElementById('chartInput');
            const pasteBtn = document.getElementById('pasteBtn');
            const pasteStatus = document.getElementById('pasteStatus');
            const imagePreview = document.getElementById('imagePreview');
            const previewImg = document.getElementById('previewImg');
            const removeImgBtn = document.getElementById('removeImgBtn');
            
            let currentImageData = null;
            
            // Show preview when file is selected
            chartInput.addEventListener('change', function(e) {
                if (this.files && this.files[0]) {
                    const reader = new FileReader();
                    reader.onload = function(ev) {
                        previewImg.src = ev.target.result;
                        imagePreview.style.display = 'block';
                        pasteStatus.style.display = 'none';
                        currentImageData = ev.target.result;
                    };
                    reader.readAsDataURL(this.files[0]);
                }
            });
            
            // Handle paste from clipboard
            async function pasteFromClipboard() {
                try {
                    const clipboardItems = await navigator.clipboard.read();
                    
                    for (const item of clipboardItems) {
                        if (item.types.some(type => type.startsWith('image/'))) {
                            const blob = await item.getType(item.types.find(type => type.startsWith('image/')));
                            const ext = blob.type.split('/')[1] || 'png';
                            const fileName = `pasted-image.${ext}`;
                            const file = new File([blob], fileName, { type: blob.type });
                            
                            const dataTransfer = new DataTransfer();
                            dataTransfer.items.add(file);
                            chartInput.files = dataTransfer.files;
                            
                            const event = new Event('change', { bubbles: true });
                            chartInput.dispatchEvent(event);
                            
                            pasteStatus.textContent = '✅ Image pasted successfully!';
                            pasteStatus.style.display = 'block';
                            pasteStatus.style.color = 'var(--good)';
                            
                            setTimeout(() => {
                                pasteStatus.style.display = 'none';
                            }, 3000);
                            
                            return;
                        }
                    }
                    
                    pasteStatus.textContent = '❌ No image found in the clipboard.';
                    pasteStatus.style.display = 'block';
                    pasteStatus.style.color = '#bd2d28';
                    
                    setTimeout(() => {
                        pasteStatus.style.display = 'none';
                    }, 3000);
                    
                } catch (err) {
                    if (err.name === 'NotAllowedError' || err.name === 'SecurityError') {
                        pasteStatus.textContent = '⚠️ Clipboard access denied. Please allow clipboard access or use Choose File.';
                    } else {
                        pasteStatus.textContent = '⚠️ Clipboard paste not supported in this browser. Please use Choose File.';
                    }
                    pasteStatus.style.display = 'block';
                    pasteStatus.style.color = '#bd2d28';
                    
                    setTimeout(() => {
                        pasteStatus.style.display = 'none';
                    }, 4000);
                }
            }
            
            pasteBtn.addEventListener('click', pasteFromClipboard);
            
            document.addEventListener('keydown', function(e) {
                if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
                    const target = e.target;
                    if (target.closest && target.closest('#testForm')) {
                        if (!target.closest('input[type="text"]') && !target.closest('textarea')) {
                            e.preventDefault();
                            pasteFromClipboard();
                        }
                    }
                }
            });
            
            removeImgBtn.addEventListener('click', function() {
                chartInput.value = '';
                imagePreview.style.display = 'none';
                previewImg.src = '';
                currentImageData = null;
                pasteStatus.textContent = 'Image removed';
                pasteStatus.style.display = 'block';
                pasteStatus.style.color = 'var(--muted)';
                setTimeout(() => {
                    pasteStatus.style.display = 'none';
                }, 1500);
            });
            
            pasteBtn.addEventListener('paste', function(e) {
                e.preventDefault();
                pasteFromClipboard();
            });
            
            console.log('📋 Clipboard paste support enabled for image uploads');
        })();
        </script>
        """))
    
    def create_test(self):
        if not self.require_login():
            return
        
        data, fs = self.form()
        image = ''
        chart = fs['chart'] if 'chart' in fs else None
        
        if getattr(chart, 'filename', None):
            ext = Path(chart.filename).suffix.lower()
            allowed = {'.png', '.jpg', '.jpeg', '.webp'}
            if ext not in allowed:
                return self.send(400, page('New test', '<p>Use PNG, JPG or WebP for the chart.</p>'))
            name = secrets.token_hex(12) + ext
            (UPLOADS / name).write_bytes(chart.file.read())
            image = '/uploads/' + name
        
        db = conn()
        
        # Generate unique class code
        for _ in range(100):
            class_code = f"{secrets.randbelow(10000):04d}"
            existing = db.execute("SELECT id FROM tests WHERE class_code=?", (class_code,)).fetchone()
            if not existing:
                break
        else:
            db.close()
            return self.send(503, page('Error', 'Could not generate a unique class code. Please try again.'))
        
        cur = db.execute("""
            INSERT INTO tests(
                title, task1_title, task1_prompt, task1_image, 
                task2_title, task2_prompt, class_code, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (data.get('title', '').strip(), data.get('task1_title', '').strip(),
              data.get('task1_prompt', '').strip(), image,
              data.get('task2_title', '').strip(), data.get('task2_prompt', '').strip(),
              class_code, int(time.time())))
        db.commit()
        test_id = cur.lastrowid
        db.close()
        
        self.send(200, page('Test created', f"""
        <h1>✅ Test created successfully!</h1>
        <div class=notice>
            <h2>Class code: <strong style='font-size:42px;letter-spacing:6px;'>{class_code}</strong></h2>
            <p>Share this code with your students. Each student must enter their name when starting the test.</p>
            <p style='color:#617586;'>Students can take the test multiple times - each attempt is saved separately.</p>
        </div>
        <p><a class=button href='/admin'>Back to dashboard</a></p>
        """))
    
    # ========================================================================
    # ADMIN: GENERATE CLASS CODE
    # ========================================================================
    def generate_class_code(self, test_id):
        if not self.require_login():
            return
        
        try:
            test_id = int(test_id)
        except ValueError:
            return self.send(404, 'Not found')
        
        db = conn()
        test = db.execute("SELECT id FROM tests WHERE id=?", (test_id,)).fetchone()
        if not test:
            db.close()
            return self.send(404, 'Test not found')
        
        # Generate unique class code
        for _ in range(100):
            class_code = f"{secrets.randbelow(10000):04d}"
            existing = db.execute("SELECT id FROM tests WHERE class_code=? AND id!=?", (class_code, test_id)).fetchone()
            if not existing:
                break
        else:
            db.close()
            return self.send(503, page('Error', 'Could not generate a unique class code. Please try again.'))
        
        db.execute("UPDATE tests SET class_code=? WHERE id=?", (class_code, test_id))
        db.commit()
        db.close()
        
        self.send(200, page('Code generated', f"""
        <h1>🔄 New class code generated</h1>
        <div class=notice>
            <h2>New class code: <strong style='font-size:42px;letter-spacing:6px;'>{class_code}</strong></h2>
            <p>Share this code with your students.</p>
            <p style='color:#c33;'>⚠️ Note: This replaces the previous code for this test.</p>
        </div>
        <p><a class=button href='/admin'>Back to dashboard</a></p>
        """))
    
    # ========================================================================
    # ADMIN: TOGGLE TEST
    # ========================================================================
    def toggle_test(self, test_id):
        if not self.require_login():
            return
        
        try:
            test_id = int(test_id)
        except ValueError:
            return self.send(404, 'Not found')
        
        db = conn()
        test = db.execute("SELECT active FROM tests WHERE id=?", (test_id,)).fetchone()
        if not test:
            db.close()
            return self.send(404, 'Test not found')
        
        new_status = 0 if test['active'] else 1
        db.execute("UPDATE tests SET active=? WHERE id=?", (new_status, test_id))
        db.commit()
        db.close()
        self.redirect('/admin')
    
    # ========================================================================
    # ADMIN: AI MARKING (OpenAI Responses API)
    # ========================================================================
    def grade_submission(self, submission_id):
        if not self.require_login():
            return
        
        try:
            submission_id = int(submission_id)
        except ValueError:
            return self.send(404, 'Submission not found')
        
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            return self.send(400, page('AI marking not configured', """
            <h1>AI marking is not connected yet</h1>
            <div class=notice>Add your personal <code>OPENAI_API_KEY</code> in Terminal before starting the server.</div>
            <p><a class=button href='/admin/submissions'>Back to submissions</a></p>
            """))
        
        db = conn()
        row = db.execute("""
            SELECT s.*, t.title, t.task1_prompt, t.task2_prompt 
            FROM submissions s JOIN tests t ON t.id = s.test_id 
            WHERE s.id = ?
        """, (submission_id,)).fetchone()
        db.close()
        
        if not row:
            return self.send(404, 'Submission not found')
        
        prompt = f"""You are an experienced IELTS Writing teacher. Give constructive, supportive feedback only.

Evaluate the following student work against IELTS Writing band descriptors. Give separate estimated bands (0–9, including .5) for Task 1 and Task 2, then one overall estimated writing band. Explain the scores under: Task Achievement/Response, Coherence and Cohesion, Lexical Resource, and Grammatical Range and Accuracy. Give 3 strengths, 3 highest-priority improvements, and one short corrected example sentence.

Task 1 question:
{row['task1_prompt']}

Task 1 answer:
{row['task1_answer']}

Task 2 question:
{row['task2_prompt']}

Task 2 answer:
{row['task2_answer']}

Return plain text with clear headings."""
        
        # Use OpenAI Responses API (newer API)
        body = json.dumps({
            "model": os.environ.get('OPENAI_MODEL', 'gpt-3.5-turbo'),
            "input": prompt
        }).encode()
        
        req = urlrequest.Request(
            'https://api.openai.com/v1/responses',
            data=body,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            method='POST'
        )
        
        try:
            with urlrequest.urlopen(req, timeout=90) as response:
                result = json.loads(response.read())
            
            # Extract feedback from Responses API format
            feedback = result.get('output_text', '')
            if not feedback:
                # Fallback: try to extract from output array
                output = result.get('output', [])
                for item in output:
                    if item.get('type') == 'message':
                        content = item.get('content', [])
                        for c in content:
                            if c.get('type') == 'output_text':
                                feedback = c.get('text', '')
                                break
                    if feedback:
                        break
            
            if not feedback:
                raise ValueError('The AI returned no written feedback.')
                
        except (urlerror.URLError, urlerror.HTTPError, ValueError) as exc:
            error_msg = str(exc)
            if hasattr(exc, 'read'):
                try:
                    error_detail = json.loads(exc.read())
                    error_msg = error_detail.get('error', {}).get('message', error_msg)
                except:
                    pass
            return self.send(502, page('AI marking unavailable', f"""
            <h1>AI marking could not be completed</h1>
            <div class=notice>{esc(error_msg)}</div>
            <p><a class=button href='/admin/submissions'>Back to submissions</a></p>
            """))
        
        db = conn()
        db.execute("""
            UPDATE submissions SET 
                ai_feedback=?, ai_score=?, ai_marked_at=? 
            WHERE id=?
        """, (feedback, 'AI estimate', int(time.time()), submission_id))
        db.commit()
        db.close()
        self.redirect('/admin/submissions')
    
    # ========================================================================
    # ADMIN: SUBMISSIONS VIEW
    # ========================================================================
    def submissions(self):
        if not self.require_login():
            return
        
        db = conn()
        rows = db.execute("""
            SELECT s.*, t.title 
            FROM submissions s JOIN tests t ON t.id = s.test_id 
            ORDER BY s.submitted_at DESC
        """).fetchall()
        db.close()
        
        data = ''.join(f"""
        <tr>
            <td><b>{esc(r['student_name'])}</b></td>
            <td>{esc(r['title'])}<br><small>{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['submitted_at']))}</small></td>
            <td>{len(r['task1_answer'].split()) if r['task1_answer'] else 0} words</td>
            <td>{len(r['task2_answer'].split()) if r['task2_answer'] else 0} words</td>
            <td>
                <form method=post action='/admin/grade/{r['id']}' style='display:inline;'>
                    <button class=accent>AI mark</button>
                </form>
                {'<p class=score>✓ Feedback ready</p>' if r['ai_feedback'] else ''}
            </td>
            <td>
                <details>
                    <summary>Read answers</summary>
                    <h4>Task 1</h4>
                    <pre>{esc(r['task1_answer'])}</pre>
                    <h4>Task 2</h4>
                    <pre>{esc(r['task2_answer'])}</pre>
                    {'<h4>AI feedback (unofficial estimate)</h4><pre>'+esc(r['ai_feedback'])+'</pre>' if r['ai_feedback'] else ''}
                </details>
            </td>
        </tr>
        """ for r in rows) or '<tr><td colspan=6>No submissions yet.</td></tr>'
        
        self.send(200, page('Submissions', f"""
        <h1>📋 Student submissions</h1>
        <p>Each student is identified by their name. Students can take the same test multiple times.</p>
        <p>Each attempt has a unique identifier (UUID) for complete isolation.</p>
        <table>
            <tr><th>Student</th><th>Test</th><th>Task 1</th><th>Task 2</th><th>Feedback</th><th>Work</th></tr>
            {data}
        </table>
        """))

# ============================================================================
# START SERVER
# ============================================================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", "8080"))
    print(f"🌐 Chill IELTS is ready at http://localhost:{port}")
    print(f"📁 Data directory: {DATA}")
    print(f"💾 Database: {DB_PATH}")
    print(f"🖼️ Uploads: {UPLOADS}")
    print(f"🔒 Each student attempt has a unique UUID for complete data isolation")
    ThreadingHTTPServer(("0.0.0.0", port), App).serve_forever()
