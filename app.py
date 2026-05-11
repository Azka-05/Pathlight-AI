"""
Pathlight AI — Flask backend wrapping Azure OpenAI plus demo auth and career tools.
"""
import os, json, re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from openai import AzureOpenAI

load_dotenv(override=True)

try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None

AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT", "https://app-uoaihack6zs3w.azurewebsites.net")
AZURE_API_KEY = os.getenv("AZURE_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_API_VERSION", "2024-12-01-preview")
MODEL = os.getenv("AZURE_MODEL", "gpt-5.4")
print("USING AZURE MODEL:", MODEL)

client = AzureOpenAI(api_version=AZURE_API_VERSION, azure_endpoint=AZURE_ENDPOINT, api_key=AZURE_API_KEY or "missing-key")

app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
CORS(app, supports_credentials=True)

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("MONGO_DB_NAME", "pathlight")
_local_users_path = os.path.join(os.path.dirname(__file__), "users.local.json")
_mongo_users = None

if MONGO_URI and MongoClient:
    try:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        _mongo_client.admin.command("ping")
        _mongo_users = _mongo_client[DB_NAME]["users"]
        _mongo_users.create_index("email", unique=True)
        print("MongoDB auth connected")
    except Exception as exc:
        print("MongoDB unavailable, using local JSON auth:", exc)

def _strip_json_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

def _json_from_ai(content: str):
    content = _strip_json_fences(content)
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group(0))
        raise

def _normalise_email(email):
    return (email or "").strip().lower()

def _load_local_users():
    if not os.path.exists(_local_users_path):
        return {}
    try:
        with open(_local_users_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_local_users(users):
    with open(_local_users_path, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)

def _find_user(email):
    email = _normalise_email(email)
    if _mongo_users is not None:
        return _mongo_users.find_one({"email": email}, {"_id": 0})
    return _load_local_users().get(email)

def _safe_user(user):
    if not user: return None
    safe = dict(user)
    safe.pop("password_hash", None)
    return safe

def _validate_credentials(email, password):
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", _normalise_email(email)):
        return "Enter a valid email address."
    if not password or len(password) < 6:
        return "Password must be at least 6 characters."
    return None

@app.route("/api/auth/signup", methods=["POST"])
def auth_signup():
    data = request.json or {}
    email = _normalise_email(data.get("email"))
    password = data.get("password") or ""
    role = data.get("role") or "student"
    err = _validate_credentials(email, password)
    if err: return jsonify({"error": err}), 400
    if _find_user(email): return jsonify({"error": "Account already exists. Please sign in."}), 409
    user = {"email": email, "password_hash": generate_password_hash(password), "role": role, "name": email.split("@")[0].replace(".", " ").title(), "created_at": datetime.now(timezone.utc).isoformat()}
    if _mongo_users is not None:
        _mongo_users.insert_one(user)
    else:
        users = _load_local_users(); users[email] = user; _save_local_users(users)
    session["user_email"] = email
    session["role"] = role
    return jsonify({"ok": True, "user": _safe_user(user)})

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    email = _normalise_email(data.get("email")); password = data.get("password") or ""; role = data.get("role") or "student"
    err = _validate_credentials(email, password)
    if err: return jsonify({"error": err}), 400
    user = _find_user(email)
    if not user or not check_password_hash(user.get("password_hash", ""), password):
        return jsonify({"error": "Incorrect email or password."}), 401
    session["user_email"] = email; session["role"] = role
    safe = _safe_user(user); safe["role"] = role
    return jsonify({"ok": True, "user": safe})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear(); return jsonify({"ok": True})

@app.route("/api/auth/me")
def auth_me():
    user = _find_user(session.get("user_email")) if session.get("user_email") else None
    safe = _safe_user(user)
    if safe: safe["role"] = session.get("role", safe.get("role", "student"))
    return jsonify({"authenticated": bool(user), "user": safe})

@app.route("/api/translate-experience", methods=["POST"])
def translate_experience():
    data = request.json or {}
    raw = (data.get("experience") or "").strip()
    cv = (data.get("cv_text") or "").strip()
    target = (data.get("target_role") or "any professional role").strip()
    if not raw and not cv:
        return jsonify({"error": "Paste your experience or CV first."}), 400
    prompt = """You are Pathlight AI, an inclusive career coach. Convert lived experience and CV evidence into employability language.
Return ONLY JSON with this shape:
{"bullets":[{"skill":"Skill name","bullet":"STAR-style bullet"}],"hidden_strengths":["phrase"],"cv_keywords":["keyword"],"next_step":"one concrete next step"}
Rules: exactly 4 bullets, concise, no invented numbers, highlight retail/caring/community/uni experience as transferable skills."""
    try:
        r = client.chat.completions.create(model=MODEL, max_completion_tokens=1500, messages=[{"role":"system","content":prompt},{"role":"user","content":f"Target role: {target}\n\nExperience notes:\n{raw}\n\nCV text:\n{cv}"}])
        return jsonify(_json_from_ai(r.choices[0].message.content))
    except Exception as e:
        return jsonify({"error": f"AI call failed: {str(e)}"}), 500

@app.route("/api/future-path", methods=["POST"])
def future_path():
    data = request.json or {}; current=(data.get("current") or "").strip(); goal=(data.get("goal") or "").strip()
    if not current or not goal: return jsonify({"error":"Need both current state and 5-year goal."}), 400
    prompt = """Create a realistic 5-year roadmap for a student from a non-traditional background. Return ONLY JSON: {"destination":"...","years":[{"year":1,"title":"...","focus":"...","skills":[...],"actions":[...]}],"encouragement":"..."}. Exactly 5 years."""
    try:
        r = client.chat.completions.create(model=MODEL, max_completion_tokens=1800, messages=[{"role":"system","content":prompt},{"role":"user","content":f"Now: {current}\nGoal: {goal}"}])
        return jsonify(_json_from_ai(r.choices[0].message.content))
    except Exception as e:
        return jsonify({"error": f"AI call failed: {str(e)}"}), 500

@app.route("/api/career-twin", methods=["POST"])
def career_twin():
    data = request.json or {}
    cv=(data.get("cv") or "").strip(); dream=(data.get("dream_role") or "").strip(); skills=data.get("skills") or []
    if not cv and not dream: return jsonify({"error":"Add a CV, career notes, or dream role first."}),400
    prompt = """You are Pathlight AI. Score a student's potential, not privilege. Return ONLY JSON: {"score": number 0-100, "headline":"...", "strengths":[...], "next_step":"...", "mentor_reason":"...", "months":[{"label":"This week","title":"...","detail":"..."},{"label":"Month 1","title":"...","detail":"..."},{"label":"Month 3","title":"...","detail":"..."},{"label":"Month 6","title":"...","detail":"..."},{"label":"Month 12","title":"...","detail":"..."}]}"""
    try:
        r=client.chat.completions.create(model=MODEL,max_completion_tokens=1600,messages=[{"role":"system","content":prompt},{"role":"user","content":f"Dream role: {dream}\nSkills: {', '.join(skills)}\nCV: {cv}"}])
        return jsonify(_json_from_ai(r.choices[0].message.content))
    except Exception as e:
        return jsonify({"error": f"AI call failed: {str(e)}"}),500

INTERVIEW_PATTERNS = {
    "communication": r"\b(explain|clarify|present|stakeholder|listen|question|communicate|summarise)\b",
    "problem_solving": r"\b(debug|solve|analyse|investigate|root cause|trade[- ]?off|iterate|test)\b",
    "ownership": r"\b(owned|led|delivered|responsible|initiative|shipped|improved)\b",
    "teamwork": r"\b(team|collaborat|support|paired|mentor|feedback|cross[- ]?functional)\b",
    "learning": r"\b(learned|self[- ]?taught|adapted|curious|course|project|practice)\b",
    "resilience": r"\b(challenge|pressure|setback|resilien|overcame|difficult|balanced)\b",
    "impact": r"\b(user|customer|metric|result|impact|saved|increased|reduced|automated)\b"
}
FILLERS = r"\b(um+|uh+|like|basically|sort of|kind of|you know)\b"

@app.route("/api/interview-analysis", methods=["POST"])
def interview_analysis():
    text = (request.json or {}).get("transcript", "")
    role = (request.json or {}).get("role", "software engineer")
    if not text.strip(): return jsonify({"error":"Paste or dictate an interview answer first."}),400
    lower = text.lower(); words = re.findall(r"\b\w+\b", lower)
    hits = {k: len(re.findall(v, lower, re.I)) for k,v in INTERVIEW_PATTERNS.items()}
    filler_count = len(re.findall(FILLERS, lower, re.I))
    concrete = len(re.findall(r"\b(project|built|created|designed|tested|deployed|data|api|dashboard|customer|team)\b", lower))
    score = max(35, min(96, 48 + sum(min(v,4) for v in hits.values())*4 + min(concrete,8)*2 - filler_count*2))
    top = sorted(hits.items(), key=lambda x:x[1], reverse=True)[:3]
    missing = [k for k,v in hits.items() if v==0][:3]
    feedback = []
    if filler_count > 4: feedback.append("Reduce filler words and pause instead.")
    if concrete < 3: feedback.append("Add one concrete project, tool, result, or metric.")
    if missing: feedback.append("Try adding evidence for: " + ", ".join(missing).replace("_"," ") + ".")
    if not feedback: feedback.append("Strong answer: clear evidence, good role fit, and confident structure.")
    return jsonify({"score": score, "role": role, "keywords": hits, "top_signals": [x[0].replace('_',' ') for x in top if x[1] > 0], "filler_count": filler_count, "word_count": len(words), "feedback": feedback})

@app.route("/")
def index(): return send_from_directory(".", "index.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "azure_configured": bool(AZURE_API_KEY), "model": MODEL, "auth_store": "mongodb" if _mongo_users is not None else "local-json"})

if __name__ == "__main__":
    print("Pathlight AI running at http://localhost:5000")
    app.run(debug=True)