# database.py
import aiosqlite
from config import DB_FILE
from typing import List, Dict, Optional

async def get_connection():
    """Create and return an aiosqlite connection with row factory."""
    conn = await aiosqlite.connect(DB_FILE)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    """Create tables if not exists."""
    async with aiosqlite.connect(DB_FILE) as db:
        # Users table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                role TEXT DEFAULT 'user',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                banned INTEGER DEFAULT 0,
                target_number TEXT,
                user_phone TEXT
            )
        ''')
        # Protected numbers table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS protected_numbers (
                number TEXT PRIMARY KEY,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Settings table (for dynamic intervals)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # Indexes for performance
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_banned ON users(banned)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)')
        await db.commit()

# ------------------------------------------------------------------
# यूजर ऑपरेशंस
# ------------------------------------------------------------------
async def add_user(user_id: int, username: str = None, first_name: str = None):
    """Add a new user or ignore if already exists."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            'INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)',
            (user_id, username, first_name)
        )
        await db.commit()

async def is_admin(user_id: int) -> bool:
    """Check if user is admin or owner."""
    from config import OWNER_ID
    if user_id == OWNER_ID:
        return True
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT role FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None and row['role'] == 'admin'

async def is_owner(user_id: int) -> bool:
    """Check if user is the bot owner."""
    from config import OWNER_ID
    return user_id == OWNER_ID

async def set_admin_role(user_id: int, make_admin: bool):
    """Promote or demote a user to/from admin."""
    role = 'admin' if make_admin else 'user'
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('UPDATE users SET role = ? WHERE user_id = ?', (role, user_id))
        await db.commit()

async def ban_user(user_id: int) -> bool:
    """Ban a user. Returns True if user existed."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute('UPDATE users SET banned = 1 WHERE user_id = ?', (user_id,))
        await db.commit()
        return cursor.rowcount > 0

async def unban_user(user_id: int) -> bool:
    """Unban a user. Returns True if user existed and was banned."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute('UPDATE users SET banned = 0 WHERE user_id = ?', (user_id,))
        await db.commit()
        return cursor.rowcount > 0

async def delete_user(user_id: int) -> bool:
    """Delete user from database. Returns True if user existed."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
        await db.commit()
        return cursor.rowcount > 0

async def get_user_by_id(user_id: int) -> Optional[Dict]:
    """Get full user record by ID."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def update_user_target(user_id: int, target: str):
    """Store the target phone number for a user."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('UPDATE users SET target_number = ? WHERE user_id = ?', (target, user_id))
        await db.commit()

async def get_user_target(user_id: int) -> Optional[str]:
    """Retrieve the stored target phone number for a user."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT target_number FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row['target_number'] if row else None

async def update_user_phone(user_id: int, phone: str):
    """Store user's own phone number (for self-bombing prevention)."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('UPDATE users SET user_phone = ? WHERE user_id = ?', (phone, user_id))
        await db.commit()

async def get_user_phone(user_id: int) -> Optional[str]:
    """Retrieve user's own phone number."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT user_phone FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row['user_phone'] if row else None

async def get_all_users_paginated(page: int, per_page: int = 10) -> List[Dict]:
    """Return a page of users sorted by user_id."""
    offset = page * per_page
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            '''SELECT user_id, username, first_name, role, joined_at, banned
               FROM users ORDER BY user_id LIMIT ? OFFSET ?''',
            (per_page, offset)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_all_user_ids() -> List[int]:
    """Return list of all user IDs (for broadcast)."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT user_id FROM users') as cursor:
            rows = await cursor.fetchall()
            return [row['user_id'] for row in rows]

# ------------------------------------------------------------------
# प्रोटेक्टेड नंबर्स
# ------------------------------------------------------------------
async def add_protected_number(number: str, added_by: int) -> bool:
    """Add a phone number to protected list. Returns True if added."""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('INSERT INTO protected_numbers (number, added_by) VALUES (?, ?)', (number, added_by))
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        return False

async def remove_protected_number(number: str) -> bool:
    """Remove a phone number from protected list. Returns True if removed."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute('DELETE FROM protected_numbers WHERE number = ?', (number,))
        await db.commit()
        return cursor.rowcount > 0

async def is_protected(number: str) -> bool:
    """Check if a phone number is protected."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT 1 FROM protected_numbers WHERE number = ?', (number,)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def get_all_protected_numbers() -> List[str]:
    """Return list of all protected numbers."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT number FROM protected_numbers ORDER BY added_at DESC') as cursor:
            rows = await cursor.fetchall()
            return [row['number'] for row in rows]

# ------------------------------------------------------------------
# सेटिंग्स (डायनमिक इंटरवल)
# ------------------------------------------------------------------
async def get_settings() -> Dict:
    """Get bombing interval settings, create defaults if missing."""
    from config import DEFAULT_CALL_INTERVAL, DEFAULT_SMS_INTERVAL
    async with aiosqlite.connect(DB_FILE) as db:
        # Ensure settings table exists
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.commit()
        
        settings = {}
        async with db.execute('SELECT key, value FROM settings') as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                settings[row['key']] = int(row['value'])
        
        # Insert defaults if missing
        if 'call_interval' not in settings:
            settings['call_interval'] = DEFAULT_CALL_INTERVAL
            await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                           ('call_interval', str(DEFAULT_CALL_INTERVAL)))
        if 'sms_interval' not in settings:
            settings['sms_interval'] = DEFAULT_SMS_INTERVAL
            await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                           ('sms_interval', str(DEFAULT_SMS_INTERVAL)))
        await db.commit()
    return settings

async def update_call_interval(seconds: int):
    """Update global call API interval."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                       ('call_interval', str(seconds)))
        await db.commit()

async def update_sms_interval(seconds: int):
    """Update global SMS/WhatsApp API interval."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                       ('sms_interval', str(seconds)))
        await db.commit()
