from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
import json
import os
import hashlib
import uuid
from datetime import datetime
import requests
import csv
import io

# Load .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Firebase Admin Init ──
import firebase_admin
from firebase_admin import credentials, firestore

if not firebase_admin._apps:
    creds_json = os.getenv('FIREBASE_SERVICE_ACCOUNT')
    if creds_json:
        cred = credentials.Certificate(json.loads(creds_json))
    else:
        # fallback for local dev: place firebase-credentials.json in project root
        cred = credentials.Certificate('firebase-credentials.json')
    firebase_admin.initialize_app(cred)

db = firestore.client()

app = Flask(__name__, static_folder='static')
app.secret_key = 'newage-internal-portal-secret-2026'
CORS(app)

# ── API Keys from .env ──
NOTION_API_KEY = os.getenv('NOTION_API_KEY', '')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
GROQ_API_KEYS  = [
    os.getenv('GROQ_KEY_1', ''),
    os.getenv('GROQ_KEY_2', ''),
    os.getenv('GROQ_KEY_3', ''),
    os.getenv('GROQ_KEY_4', ''),
]
GROQ_API_KEYS = [k for k in GROQ_API_KEYS if k]

EMAIL_ADDRESS      = os.getenv('EMAIL_ADDRESS', '')
EMAIL_APP_PASSWORD = os.getenv('EMAIL_APP_PASSWORD', '')

# ══════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# ── Whitelist helpers ──
def load_whitelist():
    doc = db.collection('config').document('whitelist').get()
    if doc.exists:
        return doc.to_dict().get('emails', [])
    return []

def save_whitelist(emails):
    db.collection('config').document('whitelist').set({'emails': emails})

def is_whitelisted(email):
    whitelist = load_whitelist()
    return email.lower() in [e.lower() for e in whitelist]


def send_invite_email(to_email):
    resend_key = os.getenv('RESEND_API_KEY', '')
    if not resend_key:
        return False
    try:
        res = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {resend_key}',
                'Content-Type': 'application/json'
            },
            json={
                'from': 'New Age Portal <onboarding@resend.dev>',
                'to': [to_email],
                'subject': "You're invited to the New Age Internal AI Portal",
                'html': f'''
                <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;">
                  <h2 style="color:#1A1916;margin-bottom:8px;">You're Invited! 🎉</h2>
                  <p style="color:#6B6A65;margin-bottom:24px;">You've been invited to access the <strong>New Age Internal AI Support Portal</strong>.</p>
                  <a href="https://ai-portal-beta.vercel.app/signup" style="display:inline-block;background:#1A1916;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600;margin-bottom:24px;">Sign Up Now →</a>
                  <p style="color:#6B6A65;margin-bottom:8px;">Or copy this link:</p>
                  <p style="color:#1A1916;margin-bottom:24px;">https://ai-portal-beta.vercel.app/signup</p>
                  <p style="color:#9E9C97;font-size:0.875rem;">Use this email address to sign up: <strong>{to_email}</strong></p>
                  <hr style="border:none;border-top:1px solid #E2E0DA;margin:24px 0;" />
                  <p style="color:#9E9C97;font-size:0.75rem;">New Age Portal — Internal AI Support | newage4.com</p>
                </div>
                '''
            },
            timeout=15
        )
        return res.ok
    except Exception as e:
        print(f"Email error: {e}")
        return False

# ── User helpers ──
def load_users():
    users_ref = db.collection('users').stream()
    users = [u.to_dict() for u in users_ref]
    if not users:
        # create default admin
        default = {
            'id': '1',
            'name': 'Admin',
            'email': 'admin@newage4.com',
            'password': hashlib.sha256('Admin@1234'.encode()).hexdigest(),
            'role': 'Admin',
            'status': 'Approved',
            'created_at': '2026-01-01 00:00:00'
        }
        db.collection('users').document('1').set(default)
        # whitelist admin
        wl = load_whitelist()
        if 'admin@newage4.com' not in wl:
            wl.append('admin@newage4.com')
            save_whitelist(wl)
        return [default]
    return users

def save_user(user):
    db.collection('users').document(user['id']).set(user)

def get_user_by_email(email):
    docs = db.collection('users').where('email', '==', email).limit(1).stream()
    for doc in docs:
        return doc.to_dict()
    return None

def update_user_by_email(email, updates):
    docs = db.collection('users').where('email', '==', email).limit(1).stream()
    for doc in docs:
        db.collection('users').document(doc.id).update(updates)
        return True
    return False

# ── Knowledge helpers ──
def load_knowledge():
    docs = db.collection('knowledge').order_by('created_at').stream()
    return [d.to_dict() for d in docs]

def save_knowledge_entry(entry):
    db.collection('knowledge').document(entry['id']).set(entry)

def delete_knowledge_entry(entry_id):
    db.collection('knowledge').document(entry_id).delete()

def update_knowledge_entry(entry_id, updates):
    db.collection('knowledge').document(entry_id).update(updates)

def get_knowledge_context(query=''):
    entries = load_knowledge()
    if not entries:
        return ''
    if query:
        query_words = set(query.lower().split())
        scored = []
        for e in entries:
            title_words = set(e['title'].lower().split())
            content_lower = e['content'].lower()
            score = len(query_words & title_words) * 3
            score += sum(1 for w in query_words if w in content_lower)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        relevant = [e for _, e in scored[:3]]
    else:
        relevant = entries[:3]
    if not relevant:
        return ''
    context = '\n\n==============================\nADMIN KNOWLEDGE BASE (relevant entries)\n=============================='
    for e in relevant:
        content_trimmed = e['content'][:2000]
        context += f"\n\n[{e['title']}]\n{content_trimmed}"
    return context

# ══════════════════════════════════════════
# SERVE HTML PAGES
# ══════════════════════════════════════════

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/signup')
def signup_page():
    return send_from_directory('static', 'signup.html')

@app.route('/chat')
def chat_page():
    return send_from_directory('static', 'chat.html')

@app.route('/forgot-password')
def forgot_password_page():
    return send_from_directory('static', 'forgot-password.html')

@app.route('/change-password')
def change_password_page():
    return send_from_directory('static', 'change-password.html')

@app.route('/admin')
def admin_page():
    return send_from_directory('static', 'admin.html')



@app.route('/api/test-email', methods=['GET'])
def test_email():
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        email_address = os.getenv('EMAIL_ADDRESS', '')
        email_password = os.getenv('EMAIL_APP_PASSWORD', '')

        msg = MIMEMultipart()
        msg['From']    = email_address
        msg['To']      = 'monap1281@gmail.com'
        msg['Subject'] = 'Test'
        msg.attach(MIMEText('Test email', 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=15)
        server.starttls()
        server.login(email_address, email_password)
        server.sendmail(email_address, 'monap1281@gmail.com', msg.as_string())
        server.quit()
        return jsonify({'sent': True, 'from': email_address})
    except Exception as e:
        return jsonify({'sent': False, 'error': str(e), 'email': os.getenv('EMAIL_ADDRESS', 'NOT SET')})



# ══════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════

@app.route('/api/signup', methods=['POST'])
def signup():
    data     = request.get_json()
    name     = data.get('name', '').strip()
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')
    role     = data.get('role', 'Regular')

    if not all([name, email, password]):
        return jsonify({'success': False, 'message': 'All fields are required.'})

    if not is_whitelisted(email):
        return jsonify({'success': False, 'message': 'Your email is not authorized. Please contact your admin.'})

    existing = get_user_by_email(email)
    if existing and existing.get('status') == 'Approved':
        return jsonify({'success': False, 'message': 'This email is already registered. Please login.'})

    user_id = str(uuid.uuid4())
    new_user = {
        'id':         user_id,
        'name':       name,
        'email':      email,
        'password':   hash_password(password),
        'role':       'Regular',
        'status':     'Approved',
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_user(new_user)
    return jsonify({'success': True, 'message': 'Account created successfully! You can now login.'})


@app.route('/api/login', methods=['POST'])
def login():
    data     = request.get_json()
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')

    user = get_user_by_email(email)
    if not user:
        return jsonify({'success': False, 'message': 'No account found with this email. Please sign up first.'})

    if user['password'] != hash_password(password):
        return jsonify({'success': False, 'message': 'Incorrect password. Please try again.'})

    if user['status'] == 'Approved':
        return jsonify({
            'success': True,
            'name':    user['name'],
            'email':   user['email'],
            'role':    user['role'],
            'created_at': user.get('created_at', '')
        })
    else:
        return jsonify({'success': False, 'message': 'Your account is not approved. Contact hr@newage4.com.'})


# ══════════════════════════════════════════
# WHITELIST ROUTES
# ══════════════════════════════════════════

@app.route('/api/admin/whitelist', methods=['GET'])
def get_whitelist():
    return jsonify({'success': True, 'emails': load_whitelist()})

@app.route('/api/admin/whitelist/add', methods=['POST'])
def add_to_whitelist():
    data  = request.get_json()
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'success': False, 'message': 'Email is required.'})
    wl = load_whitelist()
    if email in wl:
        return jsonify({'success': False, 'message': 'Email already in whitelist.'})
    wl.append(email)
    save_whitelist(wl)
    email_sent = send_invite_email(email)
    msg = f'{email} added to whitelist.'
    if email_sent:
        msg += ' Invite email sent!'
    return jsonify({'success': True, 'message': msg})

@app.route('/api/admin/whitelist/upload-csv', methods=['POST'])
def upload_whitelist_csv():
    if 'csv' not in request.files:
        return jsonify({'success': False, 'message': 'No CSV file uploaded.'})
    file = request.files['csv']
    if not file.filename.endswith('.csv'):
        return jsonify({'success': False, 'message': 'Only CSV files are allowed.'})
    try:
        content = file.read().decode('utf-8')
        reader  = csv.reader(io.StringIO(content))
        emails  = []
        for row in reader:
            for cell in row:
                cell = cell.strip().lower()
                if '@' in cell and '.' in cell:
                    emails.append(cell)
        if not emails:
            return jsonify({'success': False, 'message': 'No valid emails found in CSV.'})
        wl    = load_whitelist()
        added = 0
        for e in emails:
            if e not in wl:
                wl.append(e)
                added += 1
        save_whitelist(wl)
        emails_sent = 0
        for e in wl[-added:]:
            if send_invite_email(e):
                emails_sent += 1
        msg = f'{added} emails added to whitelist.'
        if emails_sent > 0:
            msg += f' {emails_sent} invite emails sent!'
        return jsonify({'success': True, 'message': msg})
    except Exception as ex:
        return jsonify({'success': False, 'message': f'Error reading CSV: {str(ex)}'})

@app.route('/api/admin/whitelist/remove', methods=['POST'])
def remove_from_whitelist():
    data  = request.get_json()
    email = data.get('email', '').strip().lower()
    wl    = load_whitelist()
    wl    = [e for e in wl if e != email]
    save_whitelist(wl)
    return jsonify({'success': True, 'message': f'{email} removed from whitelist.'})

# ══════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════

@app.route('/api/admin/users', methods=['GET'])
def get_users():
    users = load_users()
    safe  = [{k: v for k, v in u.items() if k != 'password'} for u in users]
    return jsonify({'success': True, 'users': safe})

@app.route('/api/admin/update-role', methods=['POST'])
def update_role():
    data  = request.get_json()
    email = data.get('email', '').lower()
    role  = data.get('role')

    if role not in ['Admin', 'Regular']:
        return jsonify({'success': False, 'message': 'Invalid role.'})

    updated = update_user_by_email(email, {'role': role})
    if updated:
        return jsonify({'success': True, 'message': f'Role updated to {role}.'})
    return jsonify({'success': False, 'message': 'User not found.'})

@app.route('/api/admin/approve', methods=['POST'])
def approve_user():
    data   = request.get_json()
    email  = data.get('email', '').lower()
    action = data.get('action')
    role   = data.get('role', None)

    updates = {'status': action}
    if role:
        updates['role'] = role

    updated = update_user_by_email(email, updates)
    if updated:
        return jsonify({'success': True, 'message': f'User {action.lower()} successfully.'})
    return jsonify({'success': False, 'message': 'User not found.'})

# ══════════════════════════════════════════
# GROQ / OPENAI AI ROUTE
# ══════════════════════════════════════════

NAR_TYPES = [
    {'id':1,  'title':'General Requests',                    'category':'General',    'keywords':['general','misc','other','help','info']},
    {'id':2,  'title':'Access Request',                      'category':'IT',         'keywords':['access','permission','login','account','tool','software','system','monday','slack','drive','github','credentials']},
    {'id':3,  'title':'Leave Request',                       'category':'HR',         'keywords':['leave','vacation','time off','holiday','sick','absence','pto','break']},
    {'id':4,  'title':'Purchase Request',                    'category':'Finance',    'keywords':['buy','purchase','order','laptop','monitor','hardware','equipment','device','phone','keyboard','mouse','screen']},
    {'id':5,  'title':'Payment Request',                     'category':'Finance',    'keywords':['payment','pay','invoice','reimbursement','expense','reimburse','refund','bill','money']},
    {'id':6,  'title':'Contract Change',                     'category':'Legal',      'keywords':['contract','agreement','change','modify','update','terms','scope','role','rate','hours']},
    {'id':7,  'title':'Billing / Subscription Concern',      'category':'Finance',    'keywords':['billing','subscription','charge','overcharge','plan','renewal','pricing','overage']},
    {'id':8,  'title':'Subscription Cancellation Request',   'category':'Finance',    'keywords':['cancel','cancellation','unsubscribe','stop subscription','terminate plan']},
    {'id':9,  'title':'Data Report Blocker',                 'category':'Ops',        'keywords':['report blocked','data stuck','blocker','report issue','data error','dashboard broken']},
    {'id':10, 'title':'Data Analysis and Reporting Request', 'category':'Ops',        'keywords':['data analysis','reporting','analytics','dashboard','metrics','stats','report','insight','kpi']},
    {'id':11, 'title':'Grievance / Strategic Alignment Requests','category':'HR',     'keywords':['grievance','complaint','alignment','strategy','concern','unfair','conflict','disagreement']},
    {'id':12, 'title':'Onboard New Access',                  'category':'IT',         'keywords':['onboard','new joiner','new employee','new access','setup','new hire','joining','first day']},
    {'id':13, 'title':'Complaint',                           'category':'HR',         'keywords':['complaint','problem','unhappy','dissatisfied','bad experience','report problem']},
    {'id':14, 'title':'Offer Release Request',               'category':'HR',         'keywords':['offer letter','offer release','job offer','send offer','appointment letter']},
    {'id':15, 'title':'Contract Termination',                'category':'Legal',      'keywords':['terminate','termination','end contract','fire','exit','notice period','last day']},
    {'id':16, 'title':'Resignation Letter',                  'category':'HR',         'keywords':['resign','resignation','quit','leaving','notice','last working day','stepping down']},
    {'id':17, 'title':'Job Requisition',                     'category':'HR',         'keywords':['hire','hiring','job opening','vacancy','recruit','new position','headcount','open role']},
    {'id':18, 'title':'HR Support',                          'category':'HR',         'keywords':['hr','human resources','hr help','policy','payroll','hr query','documents','certificate']},
    {'id':19, 'title':'Project Request',                     'category':'General',    'keywords':['project','new project','start project','initiate','kick off','new work','client project']},
    {'id':20, 'title':'Automation Request',                  'category':'Ops',        'keywords':['automate','automation','workflow','bot','script','automatic','zapier','make','n8n']},
    {'id':21, 'title':'Process Change Request',              'category':'Ops',        'keywords':['process','change process','improve process','workflow change','sop','procedure']},
    {'id':22, 'title':'Root Cause Analysis',                 'category':'Ops',        'keywords':['root cause','rca','why did','investigation','post mortem','incident','what went wrong']},
    {'id':23, 'title':'Emergency Protocol',                  'category':'General',    'keywords':['emergency','urgent','critical','asap','immediately','down','outage','crisis']},
    {'id':24, 'title':'Growth Team Bug Report',              'category':'Growth',     'keywords':['bug','error','crash','broken','not working','fix','glitch','app crash','defect']},
    {'id':25, 'title':'Training Required',                   'category':'HR',         'keywords':['training','learn','course','skill','workshop','upskill','tutorial','mentorship']},
    {'id':26, 'title':'Meeting Request with CEO',            'category':'Management', 'keywords':['ceo','meeting with ceo','talk to ceo','bhargav','leadership','executive','founder']},
    {'id':27, 'title':'WFH Request',                         'category':'HR',         'keywords':['wfh','work from home','remote','work remotely','home office']},
    {'id':28, 'title':'Maintenance Task',                    'category':'Ops',        'keywords':['maintenance','maintain','upkeep','routine task','scheduled','infra','server maintenance']},
]

SYSTEM_PROMPT = """You are the internal AI assistant for New Age (Invento One Private Limited). Help team members with company questions and guide them to raise the correct NAR request on Monday.com.

==============================
ABOUT NEW AGE
==============================
- Full legal name: Invento One Private Limited | Brand: New Age | Website: newage4.com
- Founded and led by: Bhargav (CEO)
- Office: B10, MBH, Sarthana, Surat, Gujarat, India
- Phone & WhatsApp: +91 81600 78511
- General email: contact@newage4.com | HR email: hr@newage4.com
- 25+ apps | 25M+ installs | 100K+ active users | 98% satisfaction | 4.6/5 rating | 15+ awards
- Divisions: Product Division (apps) | Services Division (client AI/mobile dev)
- Remote-first: 95% remote | Flexible 8hr day | 70% overlap 10AM-6PM IST

==============================
POC CONTACTS (Slack)
==============================
DEPARTMENT CONTACTS:
- IT issues → @Christian on Slack
- HR queries (leave, resignation, hiring, training) → @Sneha on Slack
- Finance queries (payments, reimbursements, billing) → @Manav on Slack
- Contract queries (contract change, termination, legal) → @Sneha on Slack (HR handles contracts)
- Growth/bugs → @Jhem on Slack
- Operations → @Nivi on Slack

TEAM LEADERS (Slack):
- New Age 1 → @Sahana Venkatesh
- New Age Guardian → @Miss Shy
- iOS Growth → @Jhem
- iOS Product Development → @Sandeep
- Internal Support & Operations → @Nivi
- New Age Alpha → @Muneera
- New Age Nova → @Apoorva Sridevi
- New Age Business → @Sneha (acting, current leader not confirmed)
- COO Office → @Anna Joe Philip

CONTACT RULE: Always suggest the right Slack contact based on the query topic. Use @username format.
- If the user asks who handles their team or who their team lead is — respond ONLY with: "Which team are you part of? I'll tell you your exact team leader." Do not guess or list all teams.
- Never suggest a NAR for contact/info questions — only suggest NAR when user wants to DO something (request, apply, submit etc.)
- Never write in paragraphs for contact answers — keep it 1-2 lines max.
- Never invent NAR types that don't exist in the 28 NAR list.
- ONLY mention POC contacts when the user specifically asks who to contact — never randomly add contact names in product/company info answers.
- NEVER end a response with ** or any stray markdown characters — always end cleanly.
- Pay: Fortnightly | Rate revision every 6 months | Freelance/contractor structure
- Leave: 1 week paid/year (after 1yr) | Up to 3 weeks unpaid | No fixed holidays
- Overtime: New Age operates on a freelance/contractor structure with flexible hours. Overtime pay is NOT part of the standard policy. For compensation queries contact @Sneha on Slack.

ALL 28 NAR REQUEST TYPES:
{nar_list}

RESPONSE FORMAT — follow this EXACTLY for every action-related query, no exceptions:

STEP 1 — Write a natural conversational reply first, like a helpful colleague would. Then if a NAR is needed, show the form details.

RESPONSE TYPES — follow exactly:

TYPE A — Pure info/contact question (e.g. "who handles IT", "what is New Age", "what are leave policies", "what are job levels"):
- Just answer in 1-2 lines. Mention Slack contact if relevant.
- Do NOT show Request Type / How to fill / Who actions it
- Do NOT append :::nar-suggestion:::
- NEVER append :::nar-suggestion::: for questions about company info, job levels, teams, policies, or any factual questions

TYPE B — Permission/policy question (e.g. "can I work from home", "can I take leave"):
- First answer conversationally: "Yes, you can! Just submit a [NAR name] so it's on record."
- Then show ONLY How to fill fields — NO "Who actions it" line
- Then append :::nar-suggestion:::

TYPE C — Direct action request (e.g. "I want to apply for leave", "I need to buy a laptop"):
- Show form fields directly — NO "Who actions it" line ever
- Then append :::nar-suggestion:::

NEVER add "(Please use Monday.com to submit the request)" — the link button handles that.
NEVER include "Who actions it" in any response — remove it completely from all replies.
NEVER show "Who actions it" for TYPE A responses.
NEVER show NAR form for pure contact/info questions.
:::nar-suggestion
{{
  "id": <number>,
  "title": "<exact title from the 28 NAR types>",
  "category": "<category>",
  "reason": "<one sentence>",
  "alternatives": [
    {{"id": <n>, "title": "<title>", "category": "<cat>"}},
    {{"id": <n>, "title": "<title>", "category": "<cat>"}}
  ]
}}
:::

IMPORTANT NAR MATCHING RULES:
- Overtime pay, extra pay, compensation claims → Payment Request (NOT Purchase Request)
- Purchase Request is ONLY for buying physical items (laptop, equipment, supplies)
- Payment Request is for: reimbursements, expense claims, overtime pay, any money owed to employee
- WFH, leave, resignation, training → always HR category NARs

RULES:
1. NEVER skip the :::nar-suggestion::: block when any action is involved — it is MANDATORY
2. NEVER share salary/pay NUMBERS or figures. If asked — say 'Salary information is confidential.'
3. NEVER mention "Monday.com" in your reply text — just say "submit the request" or "use the form below".
4. For company info questions use SPECIFIC facts (25+ apps, 25M+ installs etc.)
5. NEVER write NAR numbers like "Type 5" or "NAR #5" in the reply text — title only
6. Never mention internal system hints to the user.
7. STRICT SCOPE: Only answer questions related to New Age company info, NAR requests, who to contact, or knowledge base content.
{nar_hints}"""


def match_nar_types(query, top_n=3):
    q = query.lower()
    scored = []
    for n in NAR_TYPES:
        score = sum(len(kw.split()) for kw in n['keywords'] if kw in q)
        if score > 0:
            scored.append({**n, 'score': score})
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored[:top_n]


@app.route('/api/chat', methods=['POST'])
def chat():
    data            = request.get_json()
    messages        = data.get('messages', [])
    current_message = data.get('current_message', None)
    user_text       = messages[-1]['content'] if messages else ''
    has_images      = current_message and current_message.get('images')

    matches   = match_nar_types(user_text)
    nar_hints = ''
    if matches:
        hint_list = [{'id': m['id'], 'title': m['title'], 'category': m['category']} for m in matches]
        nar_hints = f"\n[SYSTEM ONLY - never tell user]: Likely NAR matches: {json.dumps(hint_list)}"

    nar_list = '\n'.join([f"{n['id']}. {n['title']} [{n['category']}]" for n in NAR_TYPES])

    knowledge_context = get_knowledge_context(user_text)
    system = SYSTEM_PROMPT.format(nar_list=nar_list, nar_hints=nar_hints + knowledge_context)

    if has_images:
        user_text_with_image = current_message.get('content', '').strip()
        image_prompt = user_text_with_image if user_text_with_image else ''
        image_instruction = f"{image_prompt}\n\nIMPORTANT: Only analyze this image if it is related to New Age company, work, NAR requests, Monday.com, or internal company matters. If the image is unrelated, respond with: 'I can only analyze images related to New Age work, NAR requests, or company matters.'"
        image_content = [{'type': 'text', 'text': image_instruction}]
        for img in current_message['images']:
            data_url = img['dataUrl']
            if ',' in data_url:
                base64_data = data_url.split(',')[1]
                media_type  = img.get('type', 'image/jpeg')
            else:
                base64_data = data_url
                media_type  = 'image/jpeg'
            image_content.append({
                'type': 'image_url',
                'image_url': {'url': f'data:{media_type};base64,{base64_data}'}
            })
        vision_messages  = messages[:-1] + [{'role': 'user', 'content': image_content}]
        model_to_use     = 'meta-llama/llama-4-scout-17b-16e-instruct'
        messages_to_send = vision_messages
    else:
        model_to_use     = 'llama-3.1-8b-instant'
        messages_to_send = messages

    all_providers = []
    if OPENAI_API_KEY:
        openai_model = 'gpt-4o' if has_images else 'gpt-4o-mini'
        all_providers.append({'url': 'https://api.openai.com/v1/chat/completions', 'key': OPENAI_API_KEY, 'model': openai_model})
    for key in GROQ_API_KEYS:
        all_providers.append({'url': 'https://api.groq.com/openai/v1/chat/completions', 'key': key, 'model': model_to_use})

    for i, provider in enumerate(all_providers):
        try:
            response = requests.post(
                provider['url'],
                headers={'Authorization': f'Bearer {provider["key"]}', 'Content-Type': 'application/json'},
                json={'model': provider['model'], 'max_tokens': 600, 'temperature': 0.2,
                      'messages': [{'role': 'system', 'content': system}] + messages_to_send},
                timeout=30
            )
            try:
                result = response.json()
            except Exception:
                if i < len(all_providers) - 1:
                    continue
                return jsonify({'success': False, 'message': 'AI service unavailable. Please try again.'})

            if response.ok:
                return jsonify({'success': True, 'reply': result['choices'][0]['message']['content']})
            elif result.get('error', {}).get('code') in ('rate_limit_exceeded', 'too_many_requests') and i < len(all_providers) - 1:
                continue
            elif not response.ok and i < len(all_providers) - 1:
                continue
            else:
                return jsonify({'success': False, 'message': result.get('error', {}).get('message', 'AI error. Please try again.')})
        except Exception:
            if i < len(all_providers) - 1:
                continue
            return jsonify({'success': False, 'message': 'Could not reach AI. Please try again.'})

    return jsonify({'success': False, 'message': 'All API keys exhausted.'})


# ══════════════════════════════════════════
# KNOWLEDGE BASE ROUTES
# ══════════════════════════════════════════

@app.route('/api/knowledge', methods=['GET'])
def get_knowledge():
    return jsonify({'success': True, 'entries': load_knowledge()})

@app.route('/api/knowledge/add', methods=['POST'])
def add_knowledge():
    data    = request.get_json()
    title   = data.get('title', '').strip()
    content = data.get('content', '').strip()
    url     = data.get('url', '').strip()

    if not title:
        return jsonify({'success': False, 'message': 'Title is required.'})

    if url and not content:
        try:
            if 'notion' in url.lower():
                import re
                page_id_match = re.search(r'([a-f0-9]{32})', url.replace('-', ''))
                if not page_id_match:
                    return jsonify({'success': False, 'message': 'Could not extract Notion page ID from URL.'})
                page_id = page_id_match.group(1)
                page_id = f"{page_id[0:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:32]}"
                notion_url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
                notion_headers = {
                    'Authorization': f'Bearer {NOTION_API_KEY}',
                    'Notion-Version': '2022-06-28',
                    'Content-Type': 'application/json'
                }
                notion_res  = requests.get(notion_url, headers=notion_headers, timeout=15)
                notion_data = notion_res.json()
                if not notion_res.ok:
                    return jsonify({'success': False, 'message': f'Notion API error: {notion_data.get("message", "Unknown error")}.'})

                def is_salary_text(t):
                    tl = t.lower()
                    return any(s in tl for s in ['k inr', 'inr/month', 'salary', '₹', 'per month', 'k/month'])

                def fetch_blocks(block_id):
                    r = requests.get(f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100", headers=notion_headers, timeout=15)
                    return r.json().get('results', []) if r.ok else []

                text_parts = []

                def extract_from_block(block):
                    block_type    = block.get('type')
                    block_content = block.get(block_type, {})
                    rich_text     = block_content.get('rich_text', [])
                    line = ' '.join(rt.get('plain_text', '').strip() for rt in rich_text if rt.get('plain_text'))
                    if line.strip():
                        text_parts.append(line.strip())
                    if block_type == 'table' and block.get('has_children'):
                        for row in fetch_blocks(block['id']):
                            if row.get('type') == 'table_row':
                                cells = row['table_row'].get('cells', [])
                                cell_texts = []
                                for cell in cells:
                                    ct = ' '.join(c.get('plain_text', '').strip() for c in cell if c.get('plain_text'))
                                    if ct and not is_salary_text(ct):
                                        cell_texts.append(ct)
                                if cell_texts:
                                    text_parts.append(' | '.join(cell_texts))
                    elif block.get('has_children') and block_type != 'table':
                        for child in fetch_blocks(block['id']):
                            extract_from_block(child)

                for block in notion_data.get('results', []):
                    extract_from_block(block)

                content = '\n'.join([t for t in text_parts if not is_salary_text(t)])[:6000]
                if not content:
                    return jsonify({'success': False, 'message': 'No content found in Notion page.'})
            else:
                from urllib.request import urlopen, Request
                from html.parser import HTMLParser

                class TextExtractor(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.text = []
                        self.skip = False
                    def handle_starttag(self, tag, attrs):
                        if tag in ('script', 'style', 'nav', 'footer', 'header'):
                            self.skip = True
                    def handle_endtag(self, tag):
                        if tag in ('script', 'style', 'nav', 'footer', 'header'):
                            self.skip = False
                    def handle_data(self, data):
                        if not self.skip and data.strip():
                            self.text.append(data.strip())

                req  = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html'})
                html = urlopen(req, timeout=15).read().decode('utf-8', errors='ignore')
                parser = TextExtractor()
                parser.feed(html)
                content = ' '.join([t for t in parser.text if len(t) > 3])[:5000]

        except Exception as e:
            return jsonify({'success': False, 'message': f'Could not fetch URL: {str(e)}'})

    if not content:
        return jsonify({'success': False, 'message': 'Content or URL is required.'})

    entry = {
        'id':         str(uuid.uuid4()),
        'title':      title,
        'content':    content,
        'url':        url,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_knowledge_entry(entry)
    return jsonify({'success': True, 'message': 'Knowledge entry added successfully.'})

@app.route('/api/knowledge/upload-pdf', methods=['POST'])
def upload_pdf():
    if 'pdf' not in request.files:
        return jsonify({'success': False, 'message': 'No PDF uploaded.'})
    file  = request.files['pdf']
    title = request.form.get('title', '').strip()
    if not title:
        return jsonify({'success': False, 'message': 'Title is required.'})
    if not file.filename.endswith('.pdf'):
        return jsonify({'success': False, 'message': 'Only PDF files are allowed.'})
    try:
        import PyPDF2, io as _io
        reader = PyPDF2.PdfReader(_io.BytesIO(file.read()))
        text   = ''.join(page.extract_text() or '' for page in reader.pages).strip()[:5000]
        if not text:
            return jsonify({'success': False, 'message': 'Could not extract text from PDF.'})
        entry = {
            'id':         str(uuid.uuid4()),
            'title':      title,
            'content':    text,
            'url':        f'PDF: {file.filename}',
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        save_knowledge_entry(entry)
        return jsonify({'success': True, 'message': 'PDF uploaded successfully.'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error reading PDF: {str(e)}'})

@app.route('/api/knowledge/delete', methods=['POST'])
def delete_knowledge():
    data     = request.get_json()
    entry_id = data.get('id')
    delete_knowledge_entry(entry_id)
    return jsonify({'success': True, 'message': 'Entry deleted.'})

@app.route('/api/knowledge/edit', methods=['POST'])
def edit_knowledge():
    data     = request.get_json()
    entry_id = data.get('id')
    updates  = {}
    if 'title' in data:
        updates['title'] = data['title']
    if 'content' in data:
        updates['content'] = data['content']
    update_knowledge_entry(entry_id, updates)
    return jsonify({'success': True, 'message': 'Entry updated.'})

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data         = request.get_json()
    email        = data.get('email', '').lower()
    new_password = data.get('new_password', '')
    if not all([email, new_password]):
        return jsonify({'success': False, 'message': 'All fields are required.'})
    if len(new_password) < 6:
        return jsonify({'success': False, 'message': 'Password must be at least 6 characters.'})
    updated = update_user_by_email(email, {'password': hash_password(new_password)})
    if updated:
        return jsonify({'success': True, 'message': 'Password reset successfully!'})
    return jsonify({'success': False, 'message': 'No account found with this email.'})

@app.route('/api/change-password', methods=['POST'])
def change_password():
    data         = request.get_json()
    email        = data.get('email', '').lower()
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')
    if not all([email, old_password, new_password]):
        return jsonify({'success': False, 'message': 'All fields are required.'})
    if len(new_password) < 6:
        return jsonify({'success': False, 'message': 'New password must be at least 6 characters.'})
    user = get_user_by_email(email)
    if not user:
        return jsonify({'success': False, 'message': 'User not found.'})
    if user['password'] != hash_password(old_password):
        return jsonify({'success': False, 'message': 'Current password is incorrect.'})
    update_user_by_email(email, {'password': hash_password(new_password)})
    return jsonify({'success': True, 'message': 'Password changed successfully.'})

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=7860)
