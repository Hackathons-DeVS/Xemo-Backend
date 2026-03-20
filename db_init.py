import sqlite3

def init_db():
    conn = sqlite3.connect('study_plan.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS study_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT
        );
    ''')
    c.execute('''CREATE TABLE user_tokens (
    user_id INTEGER PRIMARY KEY,
    tokens INTEGER DEFAULT 0
);''')
    conn.commit()
    conn.close()

init_db()