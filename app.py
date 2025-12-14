from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import jwt
import bcrypt
from functools import wraps
from datetime import datetime, timedelta
import paho.mqtt.client as mqtt
import ssl
import json

# ===== CONFIG =====
SECRET_KEY = os.environ.get('SECRET_KEY', 'iot-secret-key-2024')
DATABASE_URL = os.environ.get('DATABASE_URL')
MQTT_BROKER = os.environ.get('MQTT_BROKER', '')
MQTT_PORT = int(os.environ.get('MQTT_PORT', 8883))
MQTT_USERNAME = os.environ.get('MQTT_USERNAME', '')
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
MAX_SLOTS = 20

# ===== DATABASE =====
if DATABASE_URL:
    import pg8000
    import urllib.parse
    USE_POSTGRES = True
    parsed = urllib.parse.urlparse(DATABASE_URL)
    PG_CONFIG = {
        'user': parsed.username,
        'password': parsed.password,
        'host': parsed.hostname,
        'port': parsed.port or 5432,
        'database': parsed.path[1:]
    }
    print("✅ PostgreSQL (pg8000)")
else:
    import sqlite3
    USE_POSTGRES = False
    DATABASE_PATH = "iot_database.db"
    print("✅ SQLite")

def get_db():
    if USE_POSTGRES:
        return pg8000.connect(**PG_CONFIG)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def dict_row(row, desc):
    if row is None: return None
    if USE_POSTGRES:
        return dict(zip([c[0] for c in desc], row))
    return dict(row)

def q(query, params=None, one=False, fetchall=False):
    conn = get_db()
    cur = conn.cursor()
    if USE_POSTGRES and params:
        query = query.replace('?', '%s')
    try:
        cur.execute(query, params) if params else cur.execute(query)
        if one:
            r = cur.fetchone()
            d = cur.description
            cur.close(); conn.close()
            return dict_row(r, d)
        elif fetchall:
            r = cur.fetchall()
            d = cur.description
            cur.close(); conn.close()
            return [dict_row(x, d) for x in r]
        else:
            conn.commit()
            lid = cur.lastrowid if not USE_POSTGRES else None
            cur.close(); conn.close()
            return lid
    except Exception as e:
        print(f"DB Error: {e}")
        try: cur.close(); conn.close()
        except: pass
        raise e

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    try:
        if USE_POSTGRES:
            # Create tables
            cur.execute('''CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                name TEXT, role TEXT DEFAULT 'user', avatar TEXT DEFAULT '', theme TEXT DEFAULT 'dark',
                language TEXT DEFAULT 'vi', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS slots (
                id SERIAL PRIMARY KEY, slot_number INTEGER UNIQUE NOT NULL, name TEXT NOT NULL,
                type TEXT NOT NULL, icon TEXT DEFAULT '📟', unit TEXT DEFAULT '', location TEXT DEFAULT '',
                stream_url TEXT, is_active INTEGER DEFAULT 1, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS slot_data (
                id SERIAL PRIMARY KEY, slot_number INTEGER NOT NULL, value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS camera_images (
                id SERIAL PRIMARY KEY, slot_number INTEGER UNIQUE NOT NULL, image_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS reset_codes (
                id SERIAL PRIMARY KEY, email TEXT NOT NULL, code TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS alerts (
                id SERIAL PRIMARY KEY, slot_number INTEGER NOT NULL, alert_type TEXT NOT NULL,
                value REAL NOT NULL, threshold REAL NOT NULL, message TEXT,
                is_read INTEGER DEFAULT 0, email_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            conn.commit()
            print("✅ Tables created/verified")
            
            # Migration: Add alert columns to slots table if not exist
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='slots'")
            existing_cols = [row[0] for row in cur.fetchall()]
            
            migration_needed = False
            if 'alert_min' not in existing_cols:
                cur.execute("ALTER TABLE slots ADD COLUMN alert_min REAL DEFAULT NULL")
                migration_needed = True
            if 'alert_max' not in existing_cols:
                cur.execute("ALTER TABLE slots ADD COLUMN alert_max REAL DEFAULT NULL")
                migration_needed = True
            if 'alert_enabled' not in existing_cols:
                cur.execute("ALTER TABLE slots ADD COLUMN alert_enabled INTEGER DEFAULT 0")
                migration_needed = True
            
            if migration_needed:
                conn.commit()
                print("✅ Migration: Added alert columns to slots")
            
        else:
            # SQLite
            cur.execute('''CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                name TEXT, role TEXT DEFAULT 'user', avatar TEXT DEFAULT '', theme TEXT DEFAULT 'dark',
                language TEXT DEFAULT 'vi', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, slot_number INTEGER UNIQUE NOT NULL, name TEXT NOT NULL,
                type TEXT NOT NULL, icon TEXT DEFAULT '📟', unit TEXT DEFAULT '', location TEXT DEFAULT '',
                stream_url TEXT, is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS slot_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT, slot_number INTEGER NOT NULL, value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS camera_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT, slot_number INTEGER UNIQUE NOT NULL, image_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS reset_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, code TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            cur.execute('''CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, slot_number INTEGER NOT NULL, alert_type TEXT NOT NULL,
                value REAL NOT NULL, threshold REAL NOT NULL, message TEXT,
                is_read INTEGER DEFAULT 0, email_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
            conn.commit()
            
            # Migration: Add alert columns
            cols = [row[1] for row in cur.execute("PRAGMA table_info(slots)").fetchall()]
            if 'alert_min' not in cols:
                cur.execute("ALTER TABLE slots ADD COLUMN alert_min REAL DEFAULT NULL")
            if 'alert_max' not in cols:
                cur.execute("ALTER TABLE slots ADD COLUMN alert_max REAL DEFAULT NULL")
            if 'alert_enabled' not in cols:
                cur.execute("ALTER TABLE slots ADD COLUMN alert_enabled INTEGER DEFAULT 0")
            conn.commit()
        
        # Create admin user
        if USE_POSTGRES:
            cur.execute("SELECT id FROM users WHERE email = %s", ('admin@admin.com',))
        else:
            cur.execute("SELECT id FROM users WHERE email = ?", ('admin@admin.com',))
        
        if not cur.fetchone():
            pw = bcrypt.hashpw('admin123'.encode(), bcrypt.gensalt()).decode()
            if USE_POSTGRES:
                cur.execute("INSERT INTO users (email, password_hash, name, role) VALUES (%s, %s, %s, %s)",
                           ('admin@admin.com', pw, 'Administrator', 'admin'))
            else:
                cur.execute("INSERT INTO users (email, password_hash, name, role) VALUES (?, ?, ?, ?)",
                           ('admin@admin.com', pw, 'Administrator', 'admin'))
            conn.commit()
            print("✅ Created admin: admin@admin.com / admin123")
        
        print("✅ Database OK!")
        
    except Exception as e:
        print(f"❌ Database init error: {e}")
        try:
            conn.rollback()
        except:
            pass
        raise e
    finally:
        try:
            cur.close()
            conn.close()
        except:
            pass

# ===== AUTH HELPERS =====
def hash_pw(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
def verify_pw(pw, h): return bcrypt.checkpw(pw.encode(), h.encode())
def create_token(uid, email, role):
    return jwt.encode({'user_id': uid, 'email': email, 'role': role, 'exp': datetime.utcnow() + timedelta(days=7)}, SECRET_KEY, algorithm='HS256')
def decode_token(token):
    try: return jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
    except: return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            auth = request.headers['Authorization']
            if auth.startswith('Bearer '): token = auth.split(' ')[1]
        if not token: return jsonify({'success': False, 'error': 'Token không hợp lệ'}), 401
        payload = decode_token(token)
        if not payload: return jsonify({'success': False, 'error': 'Token hết hạn'}), 401
        request.user = payload
        return f(*args, **kwargs)
    return decorated

def require_role(roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if request.user.get('role') not in roles:
                return jsonify({'success': False, 'error': 'Không có quyền'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ===== MQTT =====
mqtt_client = None
mqtt_connected = False

def on_connect(c, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print("✅ MQTT Connected!")
        c.subscribe("iot/data")
        c.subscribe("iot/camera")
        c.subscribe("iot/status")
    else:
        print(f"❌ MQTT Failed: {rc}")

def on_message(c, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        if msg.topic == "iot/data":
            slot, value = data.get('slot'), data.get('value')
            if slot and value is not None:
                q("INSERT INTO slot_data (slot_number, value) VALUES (?, ?)", (slot, str(value)))
                # Check alert threshold
                check_and_create_alert(slot, value)
        elif msg.topic == "iot/camera":
            slot, image = data.get('slot'), data.get('image')
            if slot and image:
                q("DELETE FROM camera_images WHERE slot_number = ?", (slot,))
                q("INSERT INTO camera_images (slot_number, image_data) VALUES (?, ?)", (slot, image))
    except Exception as e:
        print(f"MQTT Error: {e}")

def init_mqtt():
    global mqtt_client
    if not MQTT_BROKER: return
    try:
        mqtt_client = mqtt.Client()
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
        mqtt_client.tls_insecure_set(True)
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
        print(f"🔄 MQTT connecting to {MQTT_BROKER}...")
    except Exception as e:
        print(f"❌ MQTT Error: {e}")

def publish_control(slot, cmd):
    global mqtt_client, mqtt_connected
    if mqtt_client and mqtt_connected:
        mqtt_client.publish("iot/control", json.dumps({"slot": slot, "command": cmd}))
        return True
    return False

# ===== ALERT SYSTEM =====
def check_and_create_alert(slot_number, value):
    """Kiểm tra ngưỡng và tạo cảnh báo nếu cần"""
    try:
        val = float(value)
    except (ValueError, TypeError):
        return None
    
    try:
        slot = q("SELECT * FROM slots WHERE slot_number = ?", (slot_number,), one=True)
        if not slot:
            return None
        
        # Check if alert columns exist and are enabled
        alert_enabled = slot.get('alert_enabled')
        if not alert_enabled:
            return None
        
        alert_min = slot.get('alert_min')
        alert_max = slot.get('alert_max')
        alert_type = None
        threshold = None
        
        if alert_min is not None and val < alert_min:
            alert_type = 'low'
            threshold = alert_min
        elif alert_max is not None and val > alert_max:
            alert_type = 'high'
            threshold = alert_max
        
        if alert_type:
            message = f"⚠️ {slot['name']}: Giá trị {val}{slot.get('unit', '')} {'thấp hơn' if alert_type == 'low' else 'cao hơn'} ngưỡng {threshold}{slot.get('unit', '')}"
            q("INSERT INTO alerts (slot_number, alert_type, value, threshold, message) VALUES (?, ?, ?, ?, ?)",
              (slot_number, alert_type, val, threshold, message))
            
            # Gửi email cảnh báo cho tất cả admin
            send_alert_emails(slot, alert_type, val, threshold, message)
            
            return {'type': alert_type, 'value': val, 'threshold': threshold, 'message': message}
        
        return None
    except Exception as e:
        print(f"Alert check error: {e}")
        return None

def send_alert_emails(slot, alert_type, value, threshold, message):
    """Gửi email cảnh báo cho tất cả admin"""
    if not RESEND_API_KEY:
        print(f"⚠️ Alert (no email): {message}")
        return
    
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        
        admins = q("SELECT email, name FROM users WHERE role = 'admin'", fetchall=True)
        
        for admin in admins:
            html_content = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: {'#dc3545' if alert_type == 'high' else '#ffc107'}; color: white; padding: 20px; text-align: center;">
                    <h1>{'🔴' if alert_type == 'high' else '🟡'} Cảnh báo IoT</h1>
                </div>
                <div style="padding: 20px; background: #f8f9fa;">
                    <h2>{slot['name']}</h2>
                    <p><strong>Loại cảnh báo:</strong> {'Vượt ngưỡng cao' if alert_type == 'high' else 'Dưới ngưỡng thấp'}</p>
                    <p><strong>Giá trị hiện tại:</strong> <span style="font-size: 24px; color: {'#dc3545' if alert_type == 'high' else '#ffc107'};">{value}{slot.get('unit', '')}</span></p>
                    <p><strong>Ngưỡng:</strong> {threshold}{slot.get('unit', '')}</p>
                    <p><strong>Vị trí:</strong> {slot.get('location', 'N/A')}</p>
                    <p><strong>Thời gian:</strong> {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</p>
                </div>
                <div style="padding: 10px; background: #e9ecef; text-align: center; font-size: 12px;">
                    <p>Email tự động từ hệ thống IoT Dashboard</p>
                </div>
            </div>
            """
            
            resend.Emails.send({
                "from": "onboarding@resend.dev",
                "to": admin['email'],
                "subject": f"⚠️ Cảnh báo: {slot['name']} - {'Cao' if alert_type == 'high' else 'Thấp'}",
                "html": html_content
            })
            print(f"✅ Alert email sent to {admin['email']}")
            
    except Exception as e:
        print(f"❌ Failed to send alert email: {e}")

# ===== FLASK APP =====
app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return jsonify({"success": True, "message": "🏠 IoT Backend API", "mqtt": {"connected": mqtt_connected, "broker": MQTT_BROKER}})

# ===== API: AUTH =====
@app.route('/api/auth/register', methods=['POST'])
def api_register():
    d = request.json
    email, pw, name = d.get('email','').strip(), d.get('password',''), d.get('name','').strip()
    if not email or not pw: return jsonify({"success": False, "error": "Thiếu thông tin"}), 400
    if len(pw) < 6: return jsonify({"success": False, "error": "Mật khẩu ≥ 6 ký tự"}), 400
    try:
        q("INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)", (email, hash_pw(pw), name))
        return jsonify({"success": True, "message": "Đăng ký thành công!"}), 201
    except:
        return jsonify({"success": False, "error": "Email đã tồn tại"}), 400

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.json
    email, pw = d.get('email','').strip(), d.get('password','')
    user = q("SELECT * FROM users WHERE email = ?", (email,), one=True)
    if not user: return jsonify({"success": False, "error": "Email không tồn tại"}), 401
    if not verify_pw(pw, user['password_hash']): return jsonify({"success": False, "error": "Sai mật khẩu"}), 401
    token = create_token(user['id'], user['email'], user['role'])
    return jsonify({"success": True, "token": token, "user": {
        "id": user['id'], "email": user['email'], "name": user['name'],
        "role": user['role'], "theme": user.get('theme','dark'), "language": user.get('language','vi')
    }}), 200

@app.route('/api/auth/forgot-password', methods=['POST'])
def api_forgot_password():
    import random
    email = request.json.get('email','').strip()
    user = q("SELECT id FROM users WHERE email = ?", (email,), one=True)
    if not user: return jsonify({"success": False, "error": "Email không tồn tại"}), 400
    code = str(random.randint(100000, 999999))
    q("DELETE FROM reset_codes WHERE email = ?", (email,))
    if USE_POSTGRES:
        q("INSERT INTO reset_codes (email, code, expires_at) VALUES (?, ?, CURRENT_TIMESTAMP + INTERVAL '15 minutes')", (email, code))
    else:
        q("INSERT INTO reset_codes (email, code, expires_at) VALUES (?, ?, datetime('now', '+15 minutes'))", (email, code))
    
    if RESEND_API_KEY:
        try:
            import resend
            resend.api_key = RESEND_API_KEY
            resend.Emails.send({"from": "onboarding@resend.dev", "to": email,
                "subject": "🔑 Mã reset - IoT", "html": f"<h2>Mã: <b>{code}</b></h2><p>Hết hạn 15 phút</p>"})
        except: pass
    return jsonify({"success": True, "message": "Đã gửi mã!", "code": code if not RESEND_API_KEY else None}), 200

@app.route('/api/auth/reset-password', methods=['POST'])
def api_reset_password():
    d = request.json
    email, code, new_pw = d.get('email',''), d.get('code',''), d.get('new_password','')
    if len(new_pw) < 6: return jsonify({"success": False, "error": "Mật khẩu ≥ 6 ký tự"}), 400
    if USE_POSTGRES:
        r = q("SELECT * FROM reset_codes WHERE email = ? AND code = ? AND expires_at > CURRENT_TIMESTAMP", (email, code), one=True)
    else:
        r = q("SELECT * FROM reset_codes WHERE email = ? AND code = ? AND expires_at > datetime('now')", (email, code), one=True)
    if not r: return jsonify({"success": False, "error": "Mã sai/hết hạn"}), 400
    q("UPDATE users SET password_hash = ? WHERE email = ?", (hash_pw(new_pw), email))
    q("DELETE FROM reset_codes WHERE email = ?", (email,))
    return jsonify({"success": True, "message": "Đổi mật khẩu thành công!"}), 200

# ===== API: USER =====
@app.route('/api/user/profile', methods=['GET'])
@require_auth
def api_get_profile():
    user = q("SELECT id, email, name, role, avatar, theme, language FROM users WHERE id = ?", (request.user['user_id'],), one=True)
    return jsonify({"success": True, "data": user}), 200

@app.route('/api/user/profile', methods=['PUT'])
@require_auth
def api_update_profile():
    d = request.json
    q("UPDATE users SET name=COALESCE(?,name), avatar=COALESCE(?,avatar), theme=COALESCE(?,theme), language=COALESCE(?,language) WHERE id=?",
      (d.get('name'), d.get('avatar'), d.get('theme'), d.get('language'), request.user['user_id']))
    user = q("SELECT id, email, name, role, avatar, theme, language FROM users WHERE id = ?", (request.user['user_id'],), one=True)
    return jsonify({"success": True, "data": user}), 200

@app.route('/api/user/password', methods=['PUT'])
@require_auth
def api_change_password():
    d = request.json
    old_pw, new_pw = d.get('old_password',''), d.get('new_password','')
    if len(new_pw) < 6: return jsonify({"success": False, "error": "Mật khẩu ≥ 6 ký tự"}), 400
    user = q("SELECT password_hash FROM users WHERE id = ?", (request.user['user_id'],), one=True)
    if not verify_pw(old_pw, user['password_hash']): return jsonify({"success": False, "error": "Mật khẩu cũ sai"}), 400
    q("UPDATE users SET password_hash = ? WHERE id = ?", (hash_pw(new_pw), request.user['user_id']))
    return jsonify({"success": True, "message": "Đổi mật khẩu thành công!"}), 200

# ===== API: SLOTS =====
@app.route('/api/slots', methods=['GET'])
@require_auth
def api_get_slots():
    return jsonify({"success": True, "data": q("SELECT * FROM slots WHERE is_active = 1 ORDER BY slot_number", fetchall=True)}), 200

@app.route('/api/slots/available', methods=['GET'])
@require_auth
@require_role(['admin'])
def api_available_slots():
    slots = q("SELECT slot_number FROM slots WHERE is_active = 1", fetchall=True)
    used = set(s['slot_number'] for s in slots)
    return jsonify({"success": True, "data": [i for i in range(1, MAX_SLOTS + 1) if i not in used]}), 200

@app.route('/api/slots/<int:num>', methods=['GET'])
@require_auth
def api_get_slot(num):
    s = q("SELECT * FROM slots WHERE slot_number = ?", (num,), one=True)
    if not s: return jsonify({"success": False, "error": "Không tồn tại"}), 404
    return jsonify({"success": True, "data": s}), 200

@app.route('/api/slots', methods=['POST'])
@require_auth
@require_role(['admin'])
def api_create_slot():
    d = request.json
    num, name, stype = d.get('slot_number'), d.get('name','').strip(), d.get('type','value')
    if not num or not name: return jsonify({"success": False, "error": "Thiếu thông tin"}), 400
    if num < 1 or num > MAX_SLOTS: return jsonify({"success": False, "error": f"Slot 1-{MAX_SLOTS}"}), 400
    
    # Alert settings (chỉ cho type value hoặc chart)
    alert_min = d.get('alert_min')
    alert_max = d.get('alert_max')
    alert_enabled = 1 if d.get('alert_enabled') and (alert_min is not None or alert_max is not None) else 0
    
    try:
        q("INSERT INTO slots (slot_number,name,type,icon,unit,location,stream_url,alert_min,alert_max,alert_enabled) VALUES (?,?,?,?,?,?,?,?,?,?)",
          (num, name, stype, d.get('icon','📟'), d.get('unit',''), d.get('location',''), d.get('stream_url',''),
           alert_min, alert_max, alert_enabled))
        return jsonify({"success": True, "message": f"Tạo Slot {num} thành công"}), 201
    except:
        return jsonify({"success": False, "error": f"Slot {num} đã tồn tại"}), 400

@app.route('/api/slots/<int:num>', methods=['PUT'])
@require_auth
@require_role(['admin'])
def api_update_slot(num):
    d = request.json
    
    # Alert settings
    alert_min = d.get('alert_min')
    alert_max = d.get('alert_max')
    alert_enabled = d.get('alert_enabled')
    
    q("""UPDATE slots SET name=COALESCE(?,name), type=COALESCE(?,type), icon=COALESCE(?,icon), 
        unit=COALESCE(?,unit), location=COALESCE(?,location), stream_url=COALESCE(?,stream_url),
        alert_min=?, alert_max=?, alert_enabled=COALESCE(?,alert_enabled) WHERE slot_number=?""",
      (d.get('name'), d.get('type'), d.get('icon'), d.get('unit'), d.get('location'), d.get('stream_url'),
       alert_min, alert_max, 1 if alert_enabled else 0 if alert_enabled is not None else None, num))
    return jsonify({"success": True}), 200

@app.route('/api/slots/<int:num>', methods=['DELETE'])
@require_auth
@require_role(['admin'])
def api_delete_slot(num):
    q("DELETE FROM slot_data WHERE slot_number = ?", (num,))
    q("DELETE FROM camera_images WHERE slot_number = ?", (num,))
    q("DELETE FROM slots WHERE slot_number = ?", (num,))
    return jsonify({"success": True}), 200

# ===== API: DATA =====
@app.route('/api/data', methods=['GET'])
@require_auth
def api_get_data():
    slots = q("SELECT slot_number FROM slots WHERE is_active = 1", fetchall=True)
    data = {}
    for s in slots:
        d = q("SELECT * FROM slot_data WHERE slot_number = ? ORDER BY created_at DESC LIMIT 1", (s['slot_number'],), one=True)
        if d: data[s['slot_number']] = d
    return jsonify({"success": True, "data": data}), 200

@app.route('/api/data/<int:num>/history', methods=['GET'])
@require_auth
def api_slot_history(num):
    limit = request.args.get('limit', 100, type=int)
    return jsonify({"success": True, "data": q("SELECT * FROM slot_data WHERE slot_number = ? ORDER BY created_at DESC LIMIT ?", (num, limit), fetchall=True)}), 200

@app.route('/api/data', methods=['POST'])
def api_post_data():
    d = request.json
    num, val = d.get('slot'), d.get('value')
    if num is None or val is None: return jsonify({"success": False}), 400
    q("INSERT INTO slot_data (slot_number, value) VALUES (?, ?)", (num, str(val)))
    # Check alert threshold
    alert = check_and_create_alert(num, val)
    return jsonify({"success": True, "alert": alert}), 201

# ===== API: CONTROL =====
@app.route('/api/control/<int:num>', methods=['POST'])
@require_auth
@require_role(['admin', 'operator'])
def api_control(num):
    cmd = request.json.get('command')
    if cmd not in [0, 1]: return jsonify({"success": False, "error": "Command 0/1"}), 400
    slot = q("SELECT * FROM slots WHERE slot_number = ? AND type = 'control'", (num,), one=True)
    if not slot: return jsonify({"success": False, "error": "Slot không hợp lệ"}), 400
    publish_control(num, cmd)
    q("INSERT INTO slot_data (slot_number, value) VALUES (?, ?)", (num, str(cmd)))
    return jsonify({"success": True, "message": f"{'BẬT' if cmd else 'TẮT'} Slot {num}"}), 200

# ===== API: CAMERA =====
@app.route('/api/camera/<int:num>', methods=['GET'])
@require_auth
def api_get_camera(num):
    slot = q("SELECT * FROM slots WHERE slot_number = ? AND type = 'camera'", (num,), one=True)
    if not slot: return jsonify({"success": False, "error": "Không hợp lệ"}), 400
    img = q("SELECT * FROM camera_images WHERE slot_number = ?", (num,), one=True)
    return jsonify({"success": True, "data": {
        "image_data": img['image_data'] if img else None,
        "stream_url": slot.get('stream_url',''),
        "created_at": img['created_at'] if img else None
    }}), 200

@app.route('/api/camera/<int:num>', methods=['POST'])
def api_post_camera(num):
    img = request.json.get('image')
    if not img: return jsonify({"success": False}), 400
    q("DELETE FROM camera_images WHERE slot_number = ?", (num,))
    q("INSERT INTO camera_images (slot_number, image_data) VALUES (?, ?)", (num, img))
    return jsonify({"success": True}), 201

# ===== API: ALERTS =====
@app.route('/api/alerts', methods=['GET'])
@require_auth
def api_get_alerts():
    try:
        limit = request.args.get('limit', 50, type=int)
        unread_only = request.args.get('unread', 'false').lower() == 'true'
        
        if unread_only:
            alerts = q("SELECT a.*, s.name as slot_name, s.unit, s.icon FROM alerts a LEFT JOIN slots s ON a.slot_number = s.slot_number WHERE a.is_read = 0 ORDER BY a.created_at DESC LIMIT ?", (limit,), fetchall=True)
        else:
            alerts = q("SELECT a.*, s.name as slot_name, s.unit, s.icon FROM alerts a LEFT JOIN slots s ON a.slot_number = s.slot_number ORDER BY a.created_at DESC LIMIT ?", (limit,), fetchall=True)
        
        unread_count = q("SELECT COUNT(*) as count FROM alerts WHERE is_read = 0", one=True)
        
        return jsonify({
            "success": True, 
            "data": alerts or [],
            "unread_count": unread_count['count'] if unread_count else 0
        }), 200
    except Exception as e:
        print(f"Alerts API error: {e}")
        return jsonify({"success": True, "data": [], "unread_count": 0}), 200

@app.route('/api/alerts/<int:alert_id>/read', methods=['PUT'])
@require_auth
def api_mark_alert_read(alert_id):
    try:
        q("UPDATE alerts SET is_read = 1 WHERE id = ?", (alert_id,))
    except:
        pass
    return jsonify({"success": True}), 200

@app.route('/api/alerts/read-all', methods=['PUT'])
@require_auth
def api_mark_all_alerts_read():
    try:
        q("UPDATE alerts SET is_read = 1 WHERE is_read = 0")
    except:
        pass
    return jsonify({"success": True}), 200

@app.route('/api/alerts/<int:alert_id>', methods=['DELETE'])
@require_auth
@require_role(['admin'])
def api_delete_alert(alert_id):
    try:
        q("DELETE FROM alerts WHERE id = ?", (alert_id,))
    except:
        pass
    return jsonify({"success": True}), 200

@app.route('/api/alerts/clear', methods=['DELETE'])
@require_auth
@require_role(['admin'])
def api_clear_alerts():
    try:
        q("DELETE FROM alerts")
    except:
        pass
    return jsonify({"success": True}), 200

# ===== API: DASHBOARD =====
@app.route('/api/dashboard/stats', methods=['GET'])
@require_auth
def api_stats():
    t = q("SELECT COUNT(*) as count FROM slots WHERE is_active = 1", one=True)
    c = q("SELECT COUNT(*) as count FROM slots WHERE is_active = 1 AND type = 'camera'", one=True)
    co = q("SELECT COUNT(*) as count FROM slots WHERE is_active = 1 AND type = 'control'", one=True)
    ch = q("SELECT COUNT(*) as count FROM slots WHERE is_active = 1 AND type = 'chart'", one=True)
    return jsonify({"success": True, "data": {
        'total_slots': t['count'], 'total_cameras': c['count'],
        'total_controls': co['count'], 'total_charts': ch['count']
    }}), 200

@app.route('/api/dashboard/full', methods=['GET'])
@require_auth
def api_full_dashboard():
    slots = q("SELECT * FROM slots WHERE is_active = 1 ORDER BY slot_number", fetchall=True)
    data = {}
    alerts_status = {}
    
    for s in slots:
        d = q("SELECT * FROM slot_data WHERE slot_number = ? ORDER BY created_at DESC LIMIT 1", (s['slot_number'],), one=True)
        if d: 
            data[s['slot_number']] = d
            # Check if current value is in alert state (safe access with .get())
            try:
                if s.get('alert_enabled') and d:
                    val = float(d['value'])
                    alert_min = s.get('alert_min')
                    alert_max = s.get('alert_max')
                    if alert_min is not None and val < alert_min:
                        alerts_status[s['slot_number']] = {'type': 'low', 'value': val, 'threshold': alert_min}
                    elif alert_max is not None and val > alert_max:
                        alerts_status[s['slot_number']] = {'type': 'high', 'value': val, 'threshold': alert_max}
            except: 
                pass
    
    t = q("SELECT COUNT(*) as count FROM slots WHERE is_active = 1", one=True)
    c = q("SELECT COUNT(*) as count FROM slots WHERE type = 'camera' AND is_active = 1", one=True)
    co = q("SELECT COUNT(*) as count FROM slots WHERE type = 'control' AND is_active = 1", one=True)
    ch = q("SELECT COUNT(*) as count FROM slots WHERE type = 'chart' AND is_active = 1", one=True)
    
    # Safe access to alerts table
    try:
        unread_alerts = q("SELECT COUNT(*) as count FROM alerts WHERE is_read = 0", one=True)
        unread_count = unread_alerts['count'] if unread_alerts else 0
    except:
        unread_count = 0
    
    return jsonify({
        "success": True, 
        "slots": slots, 
        "data": data,
        "alerts_status": alerts_status,
        "unread_alerts": unread_count,
        "stats": {
            'total_slots': t['count'], 
            'total_cameras': c['count'], 
            'total_controls': co['count'], 
            'total_charts': ch['count']
        },
        "mqtt": {"connected": mqtt_connected, "broker": MQTT_BROKER}
    }), 200

# ===== API: ADMIN =====
@app.route('/api/admin/users', methods=['GET'])
@require_auth
@require_role(['admin'])
def api_get_users():
    return jsonify({"success": True, "data": q("SELECT id, email, name, role, created_at FROM users ORDER BY created_at DESC", fetchall=True)}), 200

@app.route('/api/admin/users/<int:uid>/role', methods=['PUT'])
@require_auth
@require_role(['admin'])
def api_change_role(uid):
    role = request.json.get('role')
    if role not in ['admin','operator','user']: return jsonify({"success": False}), 400
    q("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    return jsonify({"success": True}), 200

@app.route('/api/admin/users/<int:uid>/reset-password', methods=['POST'])
@require_auth
@require_role(['admin'])
def api_admin_reset_pw(uid):
    new_pw = request.json.get('new_password', '123456')
    q("UPDATE users SET password_hash = ? WHERE id = ?", (hash_pw(new_pw), uid))
    return jsonify({"success": True, "message": f"Reset: {new_pw}"}), 200

@app.route('/api/admin/users/<int:uid>', methods=['DELETE'])
@require_auth
@require_role(['admin'])
def api_delete_user(uid):
    user = q("SELECT email FROM users WHERE id = ?", (uid,), one=True)
    if user and user['email'] == 'admin@admin.com':
        return jsonify({"success": False, "error": "Không thể xóa admin gốc"}), 400
    q("DELETE FROM users WHERE id = ?", (uid,))
    return jsonify({"success": True}), 200

@app.route('/api/mqtt/status', methods=['GET'])
@require_auth
def api_mqtt_status():
    return jsonify({"success": True, "data": {"connected": mqtt_connected, "broker": MQTT_BROKER}}), 200

# ===== START =====
init_db()
init_mqtt()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True)
