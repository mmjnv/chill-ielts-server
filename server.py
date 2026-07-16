#!/usr/bin/env python3
"""Chill IELTS: small, self-contained teacher dashboard and task server.

No third-party packages are needed.  It stores data in SQLite beside this file.
For public use, put it behind HTTPS (for example on Render, Railway, or a school server).
"""
from __future__ import annotations
import cgi, hashlib, hmac, json, os, secrets, sqlite3, time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib import request as urlrequest, error as urlerror

HERE = Path(__file__).resolve().parent
ROOT = HERE
DATA = HERE / "data"
UPLOADS = DATA / "uploads"
DB = DATA / "chill_ielts.sqlite3"
CONFIG = DATA / "settings.json"
DATA.mkdir(exist_ok=True); UPLOADS.mkdir(exist_ok=True)

def sha(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
def load_settings():
    if CONFIG.exists(): return json.loads(CONFIG.read_text())
    password = os.environ.get("ADMIN_PASSWORD")
    secret = os.environ.get("SESSION_SECRET")
    if not password:
        password = input("Create a teacher dashboard password: ").strip()
    if not password: raise SystemExit("A dashboard password is required.")
    settings = {"password_hash": sha(password), "secret": secret or secrets.token_urlsafe(32)}
    CONFIG.write_text(json.dumps(settings)); os.chmod(CONFIG, 0o600)
    return settings
SETTINGS = load_settings()

def conn():
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS tests (id INTEGER PRIMARY KEY, title TEXT NOT NULL, task1_title TEXT NOT NULL, task1_prompt TEXT NOT NULL, task1_image TEXT, task2_title TEXT NOT NULL, task2_prompt TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS codes (id INTEGER PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, test_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, FOREIGN KEY(test_id) REFERENCES tests(id));
      CREATE TABLE IF NOT EXISTS attempts (token_hash TEXT PRIMARY KEY, code_hash TEXT NOT NULL, test_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY, test_id INTEGER NOT NULL, code_hash TEXT NOT NULL, task1_answer TEXT, task2_answer TEXT, seconds_remaining INTEGER, submitted_at INTEGER NOT NULL, UNIQUE(test_id, code_hash));
    """)
    # Safe upgrades for databases created by an earlier version of the server.
    attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
    if 'student_name' not in attempt_columns: db.execute("ALTER TABLE attempts ADD COLUMN student_name TEXT NOT NULL DEFAULT ''")
    submission_columns = {row[1] for row in db.execute("PRAGMA table_info(submissions)")}
    if 'student_name' not in submission_columns: db.execute("ALTER TABLE submissions ADD COLUMN student_name TEXT NOT NULL DEFAULT ''")
    if 'ai_feedback' not in submission_columns: db.execute("ALTER TABLE submissions ADD COLUMN ai_feedback TEXT")
    if 'ai_score' not in submission_columns: db.execute("ALTER TABLE submissions ADD COLUMN ai_score TEXT")
    if 'ai_marked_at' not in submission_columns: db.execute("ALTER TABLE submissions ADD COLUMN ai_marked_at INTEGER")
    return db

def esc(value):
    import html; return html.escape(str(value or ""), quote=True)
def page(title, body): return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>:root{{--navy:#123758;--orange:#ff5a16;--ink:#18344e;--muted:#617586}}*{{box-sizing:border-box}}body{{font:15px Arial;background:#f3f7fa;color:var(--ink);margin:0}}header{{background:#fff;padding:15px max(5%,22px);display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #d7e0e8;box-shadow:0 1px 5px #1231}}header b{{font-size:18px}}header a{{color:var(--navy);text-decoration:none;margin-left:12px;font-weight:bold}}main{{max-width:1120px;margin:32px auto;background:#fff;padding:30px;border-radius:10px;box-shadow:0 3px 20px #1232}}h1{{margin-top:0}}input,textarea,select{{width:100%;padding:11px;margin:5px 0 16px;border:1px solid #b8c7d3;border-radius:5px;font:inherit}}textarea{{min-height:110px}}button,.button{{background:var(--navy);color:#fff;border:0;padding:11px 16px;border-radius:5px;font-weight:bold;text-decoration:none;cursor:pointer;display:inline-block}}button:hover,.button:hover{{filter:brightness(1.1)}}.accent{{background:var(--orange)}}.danger{{background:#bd2d28}}.msg{{padding:12px;background:#e5f5ee;border-radius:5px}}.notice{{padding:12px;background:#fff4e9;border-left:4px solid var(--orange);border-radius:4px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #dde5eb;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:var(--muted);letter-spacing:.05em}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:20px 0}}.stat{{padding:18px;border:1px solid #d8e2ea;border-radius:8px;background:#fbfdfe}}.stat strong{{font-size:28px;display:block;color:var(--navy)}}pre{{white-space:pre-wrap;font:14px/1.55 Arial}}.score{{color:#087e54;font-weight:bold}}@media(max-width:650px){{.grid,.stats{{grid-template-columns:1fr}}header{{align-items:flex-start;gap:10px;flex-direction:column}}main{{margin:12px;padding:20px}}table{{font-size:13px}}}}</style></head><body><header><b>Chill IELTS · Teacher dashboard</b><span><a href='/admin'>Tests</a> <a href='/admin/submissions'>Submissions</a> <a href='/admin/logout'>Log out</a></span></header><main>{body}</main></body></html>"""

class App(BaseHTTPRequestHandler):
    server_version = "ChillIELTS/1.0"
    def log_message(self, fmt, *args): print(time.strftime("%H:%M:%S"), fmt % args)
    def send(self, status, body, content_type="text/html; charset=utf-8", headers=None):
        raw = body if isinstance(body, bytes) else body.encode(); self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.send_header("X-Content-Type-Options", "nosniff");
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(raw)
    def json(self, status, value): self.send(status, json.dumps(value), "application/json; charset=utf-8", {"Cache-Control":"no-store"})
    def form(self):
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"): return json.loads(self.rfile.read(int(self.headers.get("Content-Length",0)) or 0))
        fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD":"POST","CONTENT_TYPE":ctype})
        return {k: fs.getvalue(k) for k in fs.keys()}, fs
    def logged_in(self):
        cookie=SimpleCookie(self.headers.get("Cookie")); token=cookie.get("teacher_session")
        if not token: return False
        try:
            val, sig = token.value.split(".",1); expected=hmac.new(SETTINGS["secret"].encode(),val.encode(),hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig,expected) and int(val)>time.time()
        except Exception: return False
    def require_login(self):
        if self.logged_in(): return True
        self.redirect('/admin/login'); return False
    def redirect(self, where): self.send(HTTPStatus.SEE_OTHER, b"", headers={"Location":where})
    
    def static(self, path):
        target = (ROOT / path.lstrip('/')).resolve()
        
        # Prevent path traversal
        if not str(target).startswith(str(ROOT.resolve())):
            return self.send(404, "Not found")
        
        if not target.is_file():
            print("Missing file:", target)
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
        
        self.send(
            200,
            target.read_bytes(),
            types.get(target.suffix.lower(), 'application/octet-stream')
        )
    
    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)

        # API endpoints
        if p.path == '/api/test':
            return self.api_test(q.get('code', [''])[0], q.get('name', [''])[0])

        # Static file serving for uploads
        if p.path.startswith('/uploads/'):
            return self.static('data' + p.path)

        # Static files in root
        if p.path == '/' or p.path == '/practice':
            # Check if the file exists, serve it
            html_path = ROOT / 'ielts-writing-exam.html'
            if html_path.exists():
                return self.send(200, html_path.read_text(), 'text/html; charset=utf-8')
            else:
                return self.send(404, "File not found")

        if p.path == '/chill-ielts-logo.png':
            img_path = ROOT / 'chill-ielts-logo.png'
            if img_path.exists():
                return self.send(200, img_path.read_bytes(), 'image/png')
            else:
                return self.send(404, "File not found")

        # Admin routes
        if p.path == '/admin/login':
            return self.send(
                200,
                page(
                    'Teacher sign in',
                    "<h1>Teacher sign in</h1><form method=post><label>Password</label><input name=password type=password autofocus required><button>Sign in</button></form>"
                )
            )

        if p.path == '/admin/logout':
            return self.send(
                303,
                b'',
                headers={
                    'Location': '/admin/login',
                    'Set-Cookie': 'teacher_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict'
                }
            )

        if p.path == '/admin':
            return self.dashboard()

        if p.path == '/admin/new':
            return self.new_test()

        if p.path == '/admin/submissions':
            return self.submissions()

        # Serve static files from the current directory
        # This handles CSS, JS, and other static assets
        static_extensions = {'.css', '.js', '.ico', '.txt', '.xml', '.json'}
        if Path(ROOT / p.path.lstrip('/')).exists() and Path(ROOT / p.path.lstrip('/')).suffix in static_extensions:
            return self.static(p.path)

        return self.send(404, "Not found")
    
    def do_POST(self):
        p=urlparse(self.path)
        if p.path=='/api/submissions': return self.api_submission()
        if p.path=='/admin/login': return self.login()
        if p.path=='/admin/new': return self.create_test()
        if p.path.startswith('/admin/code/'): return self.add_code(p.path.rsplit('/',1)[1])
        if p.path.startswith('/admin/grade/'): return self.grade_submission(p.path.rsplit('/',1)[1])
        return self.send(404,"Not found")
    
    def api_test(self, raw_code, raw_name=''):
        code=raw_code.strip().upper()
        student_name=raw_name.strip()[:80]
        if not code: return self.json(400,{"error":"An access code is required."})
        if not student_name: return self.json(400,{"error":"Please enter your full name."})
        db=conn(); row=db.execute("SELECT t.* FROM codes c JOIN tests t ON t.id=c.test_id WHERE c.code_hash=? AND c.active=1 AND t.active=1",(sha(code),)).fetchone(); db.close()
        if not row: return self.json(404,{"error":"This access code is not available."})
        token=secrets.token_urlsafe(32); db=conn(); db.execute("DELETE FROM attempts WHERE expires_at<?",(int(time.time()),)); db.execute("INSERT OR REPLACE INTO attempts(token_hash,code_hash,test_id,expires_at,student_name) VALUES (?,?,?,?,?)",(sha(token),sha(code),row['id'],int(time.time())+7200,student_name)); db.commit(); db.close()
        self.json(200,{"title":row['title'],"attemptToken":token,"task1":{"title":row['task1_title'],"prompt":row['task1_prompt'],"imageUrl":row['task1_image'] or ''},"task2":{"title":row['task2_title'],"prompt":row['task2_prompt']}})
    
    def api_submission(self):
        try: data=self.form()
        except Exception: return self.json(400,{"error":"Invalid submission."})
        token=str(data.get('attemptToken','')); db=conn(); attempt=db.execute("SELECT * FROM attempts WHERE token_hash=? AND expires_at>?",(sha(token),int(time.time()))).fetchone()
        if not attempt: db.close(); return self.json(403,{"error":"Your test session has expired."})
        db.execute("INSERT INTO submissions(test_id,code_hash,student_name,task1_answer,task2_answer,seconds_remaining,submitted_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(test_id,code_hash) DO UPDATE SET student_name=excluded.student_name,task1_answer=excluded.task1_answer,task2_answer=excluded.task2_answer,seconds_remaining=excluded.seconds_remaining,submitted_at=excluded.submitted_at",(attempt['test_id'],attempt['code_hash'],attempt['student_name'],str(data.get('task1Answer','')),str(data.get('task2Answer','')),int(data.get('secondsRemaining',0) or 0),int(time.time())))
        db.commit(); db.close(); self.json(200,{"ok":True})
    
    def login(self):
        data,_=self.form(); password=str(data.get('password',''))
        if not hmac.compare_digest(sha(password),SETTINGS['password_hash']): return self.send(401,page('Teacher sign in',"<h1>Teacher sign in</h1><p class=msg>Incorrect password.</p><form method=post><input name=password type=password required><button>Sign in</button></form>"))
        expiry=str(int(time.time())+28800); sig=hmac.new(SETTINGS['secret'].encode(),expiry.encode(),hashlib.sha256).hexdigest(); self.send(303,b'',headers={'Location':'/admin','Set-Cookie':f'teacher_session={expiry}.{sig}; Path=/; HttpOnly; SameSite=Strict'})
    
    def dashboard(self):
        if not self.require_login(): return
        db=conn(); tests=db.execute("SELECT t.*,count(c.id) codes FROM tests t LEFT JOIN codes c ON c.test_id=t.id GROUP BY t.id ORDER BY t.id DESC").fetchall(); submission_count=db.execute("SELECT count(*) FROM submissions").fetchone()[0]; code_count=db.execute("SELECT count(*) FROM codes").fetchone()[0]; db.close(); rows=''.join(f"<tr><td><b>{esc(x['title'])}</b><br><small>Task 1 + Task 2</small></td><td>{x['codes']}</td><td>{'Active' if x['active'] else 'Inactive'}</td><td><form method=post action='/admin/code/{x['id']}'><button>New 4-digit code</button></form></td></tr>" for x in tests) or '<tr><td colspan=4>No tests yet.</td></tr>'
        self.send(200,page('Tests',f"<h1>Your teaching workspace</h1><p>Build tests, issue short student codes, and review completed work in one place.</p><p><a class='button accent' href='/admin/new'>+ Create a new test</a> <a class=button href='/practice'>Open student page</a> <a class=button href='/admin/submissions'>Review submissions</a></p><div class=stats><div class=stat><strong>{len(tests)}</strong>Tests created</div><div class=stat><strong>{code_count}</strong>Student codes issued</div><div class=stat><strong>{submission_count}</strong>Completed submissions</div></div><table><tr><th>Test</th><th>Codes</th><th>Status</th><th>Teacher tool</th></tr>{rows}</table>"))
    
    def new_test(self):
        if not self.require_login(): return
        self.send(200,page('New test',"""<h1>Create a test</h1><form method=post enctype='multipart/form-data'><label>Test title</label><input name=title required placeholder='Academic Writing Practice 1'><div class=grid><div><h2>Task 1</h2><label>Title</label><input name=task1_title value='Describe the information' required><label>Instructions / question</label><textarea name=task1_prompt required></textarea><label>Chart image (PNG, JPG or WebP)</label><input name=chart type=file accept='image/png,image/jpeg,image/webp'></div><div><h2>Task 2</h2><label>Title</label><input name=task2_title value='Discuss both views and give your opinion' required><label>Instructions / question</label><textarea name=task2_prompt required></textarea></div></div><button>Create test</button></form>"""))
    
    def create_test(self):
        if not self.require_login(): return
        data,fs=self.form(); image=''; chart=fs['chart'] if 'chart' in fs else None
        # cgi.FieldStorage deliberately refuses truth-value checks; an image is optional.
        if getattr(chart, 'filename', None):
            ext=Path(chart.filename).suffix.lower(); allowed={'.png','.jpg','.jpeg','.webp'}
            if ext not in allowed: return self.send(400,page('New test','<p>Use PNG, JPG or WebP for the chart.</p>'))
            name=secrets.token_hex(12)+ext; (UPLOADS/name).write_bytes(chart.file.read()); image='/uploads/'+name
        db=conn(); cur=db.execute("INSERT INTO tests(title,task1_title,task1_prompt,task1_image,task2_title,task2_prompt,created_at) VALUES(?,?,?,?,?,?,?)",(data.get('title','').strip(),data.get('task1_title','').strip(),data.get('task1_prompt','').strip(),image,data.get('task2_title','').strip(),data.get('task2_prompt','').strip(),int(time.time()))); db.commit(); test_id=cur.lastrowid; db.close(); self.issue_code(test_id)
    
    def add_code(self,test_id):
        if not self.require_login(): return
        try: test_id=int(test_id)
        except ValueError: return self.send(404,'Not found')
        self.issue_code(test_id)
    
    def issue_code(self, test_id):
        """Create a code and show it immediately; only its secure hash is stored."""
        db=conn(); exists=db.execute("SELECT 1 FROM tests WHERE id=?",(test_id,)).fetchone()
        if not exists: db.close(); return self.send(404,'Test not found')
        for _ in range(100):
            code=f"{secrets.randbelow(10000):04d}"
            try:
                db.execute("INSERT INTO codes(code_hash,test_id,created_at) VALUES(?,?,?)",(sha(code),test_id,int(time.time()))); db.commit(); break
            except sqlite3.IntegrityError: continue
        else: db.close(); return self.send(503,'All short codes are currently in use. Please try again.')
        db.close(); self.send(200,page('Student code',f"<h1>Student access code</h1><p>Give this four-digit code to one student:</p><h2 style='font-size:42px;letter-spacing:.16em'>{code}</h2><div class=notice>For fairness, create a different code for every student.</div><p><a class=button href='/admin'>Back to tests</a></p>"))
    
    def grade_submission(self, submission_id):
        """Teacher-only AI feedback. The API key never reaches a student browser."""
        if not self.require_login(): return
        try: submission_id=int(submission_id)
        except ValueError: return self.send(404,'Submission not found')
        api_key=os.environ.get('OPENAI_API_KEY')
        if not api_key: return self.send(400,page('AI marking not configured',"<h1>AI marking is not connected yet</h1><div class=notice>Add your personal <code>OPENAI_API_KEY</code> in Terminal before starting the server. The key is kept only on the teacher server and is never shown to students.</div><p><a class=button href='/admin/submissions'>Back to submissions</a></p>"))
        db=conn(); row=db.execute("SELECT s.*,t.title,t.task1_prompt,t.task2_prompt FROM submissions s JOIN tests t ON t.id=s.test_id WHERE s.id=?",(submission_id,)).fetchone(); db.close()
        if not row: return self.send(404,'Submission not found')
        prompt=f"""You are an experienced IELTS Writing teacher. Give constructive, supportive feedback only; this is an unofficial classroom estimate, not an official IELTS result.

Evaluate the following student work against IELTS Writing band descriptors. Give separate estimated bands (0–9, including .5) for Task 1 and Task 2, then one overall estimated writing band. Explain the scores under: Task Achievement/Response, Coherence and Cohesion, Lexical Resource, and Grammatical Range and Accuracy. Give 3 strengths, 3 highest-priority improvements, and one short corrected example sentence. Do not invent problems that are not in the writing.

Task 1 question:\n{row['task1_prompt']}\n\nTask 1 answer:\n{row['task1_answer']}\n\nTask 2 question:\n{row['task2_prompt']}\n\nTask 2 answer:\n{row['task2_answer']}\n\nReturn plain text with clear headings."""
        body=json.dumps({"model":os.environ.get('OPENAI_MODEL','gpt-5.4-mini'),"input":prompt}).encode()
        req=urlrequest.Request('https://api.openai.com/v1/responses',data=body,headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json'},method='POST')
        try:
            with urlrequest.urlopen(req,timeout=90) as response: result=json.loads(response.read())
            feedback=result.get('output_text') or ''.join(item.get('text','') for block in result.get('output',[]) for item in block.get('content',[]) if item.get('type')=='output_text')
            if not feedback: raise ValueError('The AI returned no written feedback.')
        except (urlerror.URLError, urlerror.HTTPError, ValueError) as exc:
            return self.send(502,page('AI marking unavailable',f"<h1>AI marking could not be completed</h1><div class=notice>{esc(str(exc))}</div><p>Check your API key and internet connection, then try again.</p><p><a class=button href='/admin/submissions'>Back to submissions</a></p>"))
        score='AI estimate'; db=conn(); db.execute("UPDATE submissions SET ai_feedback=?,ai_score=?,ai_marked_at=? WHERE id=?",(feedback,score,int(time.time()),submission_id)); db.commit(); db.close(); self.redirect('/admin/submissions')
    
    def submissions(self):
        if not self.require_login(): return
        db=conn(); rows=db.execute("SELECT s.*,t.title FROM submissions s JOIN tests t ON t.id=s.test_id ORDER BY s.submitted_at DESC").fetchall(); db.close(); data=''.join(f"<tr><td><b>{esc(r['student_name'])}</b></td><td>{esc(r['title'])}<br><small>{time.strftime('%Y-%m-%d %H:%M',time.localtime(r['submitted_at']))}</small></td><td>{len(r['task1_answer'].split())} words</td><td>{len(r['task2_answer'].split())} words</td><td><form method=post action='/admin/grade/{r['id']}'><button class=accent>AI mark</button></form>{'<p class=score>AI feedback saved</p>' if r['ai_feedback'] else ''}</td><td><details><summary>Read answers and feedback</summary><h4>Task 1</h4><pre>{esc(r['task1_answer'])}</pre><h4>Task 2</h4><pre>{esc(r['task2_answer'])}</pre>{'<h4>AI feedback (unofficial estimate)</h4><pre>'+esc(r['ai_feedback'])+'</pre>' if r['ai_feedback'] else ''}</details></td></tr>" for r in rows) or '<tr><td colspan=6>No submissions yet.</td></tr>'
        self.send(200,page('Submissions',f"<h1>Student submissions</h1><p>Use AI marking as a second opinion; it is an unofficial teaching aid, not an official IELTS score.</p><table><tr><th>Student</th><th>Test</th><th>Task 1</th><th>Task 2</th><th>Feedback tool</th><th>Work</th></tr>{data}</table>"))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", "8080"))
    print(f"Chill IELTS is ready at http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), App).serve_forever()