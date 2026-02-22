import streamlit as st
import sqlite3
import pandas as pd
import datetime
import calendar
import plotly.express as px
import uuid 
import os

# --- Configuration ---
DB_NAME = "gst_portal_v20.db" 
STATUS_OPTIONS = ["Pending", "Working", "Review Pending", "Client Pending", "Payment Pending", "Return Filed", "NA"]

st.set_page_config(page_title="GST Compliance Portal", layout="wide")

def generate_months_for_fy(fy_string):
    start_year = int(fy_string.split("-")[0])
    months = []
    for m in range(4, 13): months.append(datetime.date(start_year, m, 1).strftime("%B %Y"))
    for m in range(1, 4): months.append(datetime.date(start_year + 1, m, 1).strftime("%B %Y"))
    return months

@st.cache_data
def get_period_list():
    today = datetime.date.today()
    dates = pd.date_range(start=today - pd.DateOffset(months=12), periods=24, freq='MS')
    periods = [d.strftime("%B %Y") for d in dates]
    periods.reverse()
    return periods
PERIOD_OPTIONS = get_period_list()

# --- 1. Database & Audit Setup ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS organizations (org_name TEXT PRIMARY KEY)''')
    c.execute("INSERT OR IGNORE INTO organizations VALUES ('Default Organization')")
    
    c.execute('''CREATE TABLE IF NOT EXISTS roles (
        role_name TEXT PRIMARY KEY, can_manage_clients INTEGER, can_manage_users INTEGER, can_view_audit INTEGER, can_assign_others INTEGER)''')
    c.execute("INSERT OR IGNORE INTO roles VALUES ('Super Admin', 1, 1, 1, 1)")
    c.execute("INSERT OR IGNORE INTO roles VALUES ('Staff Member', 0, 0, 0, 0)")
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, role TEXT, session_token TEXT, last_login TEXT)''')
    c.execute("INSERT OR IGNORE INTO users VALUES ('admin', 'admin', 'Super Admin', '', '')")
    c.execute("INSERT OR IGNORE INTO users VALUES ('staff1', 'staff1', 'Staff Member', '', '')")
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('global_lock_date', '')")
    
    c.execute('''CREATE TABLE IF NOT EXISTS financial_years (fy_string TEXT PRIMARY KEY)''')
    current_year = datetime.date.today().year
    current_month = datetime.date.today().month
    default_fy = f"{current_year-1}-{current_year}" if current_month < 4 else f"{current_year}-{current_year+1}"
    c.execute("INSERT OR IGNORE INTO financial_years (fy_string) VALUES (?)", (default_fy,))
    
    c.execute('''CREATE TABLE IF NOT EXISTS frequency_master (freq_name TEXT PRIMARY KEY)''')
    c.execute("INSERT OR IGNORE INTO frequency_master VALUES ('Monthly')")
    c.execute("INSERT OR IGNORE INTO frequency_master VALUES ('QRMP (Quarterly)')")
    c.execute("INSERT OR IGNORE INTO frequency_master VALUES ('Semi-Annually')")
    c.execute("INSERT OR IGNORE INTO frequency_master VALUES ('Annually')")
    
    c.execute('''CREATE TABLE IF NOT EXISTS returns_master (
        return_name TEXT PRIMARY KEY, auto_lock_days INTEGER)''')
    c.execute("INSERT OR IGNORE INTO returns_master VALUES ('GSTR1', -1)")
    c.execute("INSERT OR IGNORE INTO returns_master VALUES ('GSTR3B', -1)")
    c.execute("INSERT OR IGNORE INTO returns_master VALUES ('GSTR9', -1)")
    
    c.execute('''CREATE TABLE IF NOT EXISTS return_due_dates (
        return_name TEXT, freq_name TEXT, due_day INTEGER, due_month_type TEXT, PRIMARY KEY(return_name, freq_name))''')
    c.execute("INSERT OR IGNORE INTO return_due_dates VALUES ('GSTR1', 'Monthly', 11, 'Next Month')")
    c.execute("INSERT OR IGNORE INTO return_due_dates VALUES ('GSTR1', 'QRMP (Quarterly)', 13, 'Next Month')")
    c.execute("INSERT OR IGNORE INTO return_due_dates VALUES ('GSTR3B', 'Monthly', 20, 'Next Month')")
    c.execute("INSERT OR IGNORE INTO return_due_dates VALUES ('GSTR3B', 'QRMP (Quarterly)', 22, 'Next Month')")
    c.execute("INSERT OR IGNORE INTO return_due_dates VALUES ('GSTR9', 'Annually', 31, 'December')")
    
    c.execute('''CREATE TABLE IF NOT EXISTS clients (
        gstin TEXT PRIMARY KEY, org_name TEXT, trade_name TEXT, legal_name TEXT, filing_frequency TEXT, 
        business_type TEXT, state TEXT, gst_username TEXT, gst_password TEXT, contact_person TEXT, contact_number TEXT, 
        custom_data TEXT, applicable_returns TEXT, client_status TEXT, left_wef TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS filings (
        id INTEGER PRIMARY KEY, gstin TEXT, return_type TEXT, period TEXT, status TEXT, assigned_to TEXT, comments TEXT, carry_forward_note TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY, timestamp TEXT, username TEXT, action TEXT, details TEXT, before_state TEXT, after_state TEXT)''')
    
    conn.commit()
    conn.close()

def log_audit(username, action, details, before_state="", after_state=""):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO audit_logs (timestamp, username, action, details, before_state, after_state) VALUES (?, ?, ?, ?, ?, ?)",
              (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), username, action, details, str(before_state), str(after_state)))
    conn.commit()
    conn.close()

# --- 2. Authentication & Secure Session Engine ---
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.username = None
    st.session_state.rights = {}
    st.session_state.session_token = None
    st.session_state.login_step = 1
    st.session_state.temp_user = None
    st.session_state.temp_role = None
    st.session_state.temp_last_login = None

def finalize_login(username, role):
    new_token = str(uuid.uuid4())
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET session_token = ?, last_login = ? WHERE username = ?", (new_token, current_time, username))
    
    c.execute("SELECT can_manage_clients, can_manage_users, can_view_audit, can_assign_others FROM roles WHERE role_name = ?", (role,))
    r_data = c.fetchone()
    conn.commit()
    conn.close()
    
    st.session_state.logged_in = True
    st.session_state.username = username
    st.session_state.session_token = new_token
    st.session_state.rights = {
        'role': role, 'can_manage_clients': bool(r_data[0]), 'can_manage_users': bool(r_data[1]),
        'can_view_audit': bool(r_data[2]), 'can_assign_others': bool(r_data[3])
    }
    st.session_state.login_step = 1
    log_audit(username, "System", "User logged in.")
    st.rerun()

def login():
    init_db()
    st.title("GST Compliance Portal")
    
    if st.session_state.login_step == 1:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submit = st.form_submit_button("Login")
            
            if submit:
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("SELECT role, session_token, last_login FROM users WHERE username = ? AND password = ?", (username, password))
                result = c.fetchone()
                conn.close()
                
                if result:
                    role, s_token, l_login = result
                    if s_token and s_token != "":
                        st.session_state.login_step = 2
                        st.session_state.temp_user = username
                        st.session_state.temp_role = role
                        st.session_state.temp_last_login = l_login
                        st.rerun()
                    else:
                        finalize_login(username, role)
                else:
                    st.error("Invalid credentials.")
                    
    elif st.session_state.login_step == 2:
        st.warning(f"⚠️ An active session already exists for user: **{st.session_state.temp_user}**.")
        st.info(f"The previous session was initiated on: **{st.session_state.temp_last_login}**")
        st.write("Logging in will immediately terminate the previous session on the other device.")
        
        col1, col2 = st.columns(2)
        if col1.button("🚨 Force Login & Override Session", type="primary"):
            log_audit(st.session_state.temp_user, "System Security", f"Session forcibly overridden.")
            finalize_login(st.session_state.temp_user, st.session_state.temp_role)
        if col2.button("Cancel"):
            st.session_state.login_step = 1
            st.rerun()

def color_status(val):
    val_str = str(val)
    if 'OVERDUE' in val_str: return 'background-color: #ffcccc; color: #cc0000; font-weight: bold; border: 2px solid #cc0000;'
    if 'Return Filed' in val_str: return 'background-color: #c6efce; color: #006100;' 
    elif 'Working' in val_str or 'Review Pending' in val_str: return 'background-color: #fff2cc; color: #b45f06;' 
    elif 'Pending' in val_str: return 'background-color: #f8cecc; color: #a10000;' 
    elif 'NA' in val_str: return 'background-color: #e7eaed; color: #6c757d;' 
    return ''

# --- 3. Main Application Routing ---
def main_app():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.date.today()
    
    # Strict Simultaneous Login Check
    c.execute("SELECT session_token FROM users WHERE username = ?", (st.session_state.username,))
    db_token_row = c.fetchone()
    if not db_token_row or db_token_row[0] != st.session_state.session_token:
        st.session_state.logged_in = False
        st.session_state.username = None
        st.session_state.session_token = None
        st.warning("⚠️ Session Terminated: Your account was just logged into from another device. This session has been closed to protect your data.")
        st.stop()
    
    all_master_returns = [row[0] for row in c.execute("SELECT return_name FROM returns_master ORDER BY return_name").fetchall()]
    all_freqs = [row[0] for row in c.execute("SELECT freq_name FROM frequency_master ORDER BY freq_name").fetchall()]
    
    due_dates_db = c.execute("SELECT return_name, freq_name, due_day, due_month_type FROM return_due_dates").fetchall()
    due_dates_dict = {}
    for r_name, f_name, d_day, m_type in due_dates_db:
        if r_name not in due_dates_dict: due_dates_dict[r_name] = {}
        due_dates_dict[r_name][f_name] = {'day': d_day, 'month_type': m_type}
        
    returns_lock_db = {row[0]: row[1] for row in c.execute("SELECT return_name, auto_lock_days FROM returns_master").fetchall()}

    def local_calc_due_date(period_str, return_type, freq):
        if not period_str: return None
        try:
            p_date = datetime.datetime.strptime(period_str, "%B %Y")
            rules = due_dates_dict.get(return_type, {})
            if not rules: return None
            
            rule = rules.get(freq)
            if not rule: rule = list(rules.values())[0]
                
            day = rule['day']
            m_type = rule['month_type']
            
            if m_type == 'Next Month': target_date = p_date + pd.DateOffset(months=1)
            elif m_type == 'Same Month': target_date = p_date
            elif m_type in list(calendar.month_name)[1:]:
                m_index = list(calendar.month_name).index(m_type)
                if m_index > p_date.month: target_date = p_date.replace(month=m_index, day=1)
                else: target_date = p_date.replace(year=p_date.year + 1, month=m_index, day=1)
            else: target_date = p_date + pd.DateOffset(months=1)
                
            last_day_of_month = calendar.monthrange(target_date.year, target_date.month)[1]
            safe_day = min(int(day), last_day_of_month)
            return target_date.replace(day=safe_day).date()
        except: return None
    
    st.sidebar.title(f"Welcome, {st.session_state.username}")
    st.sidebar.caption(f"Role: **{st.session_state.rights['role']}**")
    
    all_orgs = [row[0] for row in c.execute("SELECT org_name FROM organizations ORDER BY org_name").fetchall()]
    if not all_orgs: all_orgs = ["Default Organization"]
    selected_org = st.sidebar.selectbox("🏢 Organization Filter", all_orgs)
    st.sidebar.markdown("---")
    
    available_fys = [row[0] for row in c.execute("SELECT fy_string FROM financial_years ORDER BY fy_string DESC").fetchall()]
    selected_fy = st.sidebar.selectbox("📅 Financial Year", available_fys)
    
    months_in_fy = generate_months_for_fy(selected_fy)
    current_month_str = datetime.datetime.now().strftime("%B %Y")
    default_month_index = months_in_fy.index(current_month_str) if current_month_str in months_in_fy else 0
    selected_period = st.sidebar.selectbox("📆 Filing Period", months_in_fy, index=default_month_index)
    
    st.sidebar.markdown("---")
    
    menu = ["Monthly Dashboard", "Update Work"]
    if st.session_state.rights['can_manage_clients']: menu.append("Clients Master")
    if st.session_state.rights['can_manage_users']: menu.append("Admin Settings")
    if st.session_state.rights['can_view_audit']: menu.append("Audit Logs")
        
    choice = st.sidebar.radio("Navigation", menu)
    
    if st.sidebar.button("Logout"):
        c.execute("UPDATE users SET session_token = '' WHERE username = ?", (st.session_state.username,))
        conn.commit()
        log_audit(st.session_state.username, "System", "User logged out securely.")
        for key in list(st.session_state.keys()): del st.session_state[key]
        st.rerun()

    # --- A. Monthly Dashboard ---
    if choice == "Monthly Dashboard":
        st.title(f"Compliance Dashboard ({selected_period})")
        st.caption(f"Filtering records for Organization: **{selected_org}**")
        all_users = [row[0] for row in c.execute("SELECT username FROM users").fetchall()]
        
        st.markdown("##### 🔍 Advanced Filters")
        dash_search = st.text_input("Type to Filter (Searches Trade Name, GSTIN, and Comments)")
        
        col_df1, col_df2, col_df3, col_df4 = st.columns(4)
        dash_return = col_df1.multiselect("Return Types (Leave blank for All)", all_master_returns)
        dash_status = col_df2.multiselect("Statuses (Leave blank for All)", STATUS_OPTIONS)
        dash_status_exclude = col_df2.checkbox("Exclude selected statuses instead of including")
        dash_user = col_df3.multiselect("Assigned To (Leave blank for All)", ["Unassigned"] + all_users)
        dash_freq = col_df4.multiselect("Frequencies (Leave blank for All)", all_freqs)
        st.markdown("---")
        
        query = """SELECT c.gstin, c.trade_name, c.filing_frequency, f.period, f.return_type, f.status, f.assigned_to, f.comments, c.client_status, c.left_wef 
                   FROM clients c LEFT JOIN filings f ON c.gstin = f.gstin AND f.period = ? 
                   WHERE c.org_name = ?"""
        df = pd.read_sql_query(query, conn, params=(selected_period, selected_org))
        
        if not df.empty:
            if dash_search:
                df = df[df['trade_name'].str.contains(dash_search, case=False, na=False) | 
                        df['gstin'].str.contains(dash_search, case=False, na=False) |
                        df['comments'].str.contains(dash_search, case=False, na=False)]
            if dash_return: df = df[df['return_type'].isin(dash_return)]
            if dash_freq: df = df[df['filing_frequency'].isin(dash_freq)]
            if dash_status:
                if dash_status_exclude: df = df[~df['status'].isin(dash_status)]
                else: df = df[df['status'].isin(dash_status)]
            if dash_user:
                assigned_mask = df['assigned_to'].isin(dash_user)
                if "Unassigned" in dash_user:
                    assigned_mask = assigned_mask | (df['assigned_to'] == '') | (df['assigned_to'].isna()) | (df['assigned_to'] == 'Unassigned')
                df = df[assigned_mask]
        
        c.execute("SELECT COUNT(*) FROM filings f JOIN clients c ON f.gstin = c.gstin WHERE f.period = ? AND c.org_name = ?", (selected_period, selected_org))
        is_initialized = c.fetchone()[0] > 0
        
        if not df.empty and df['return_type'].notna().any():
            active_returns = sorted(df['return_type'].unique().tolist())
            cols = st.columns(3) 
            for i, r_type in enumerate(active_returns):
                df_r = df[df['return_type'] == r_type]
                if not df_r.empty:
                    fig = px.pie(df_r, names='status', title=f'{r_type} Status', hole=0.4, color_discrete_sequence=px.colors.qualitative.Pastel)
                    cols[i % 3].plotly_chart(fig, width='stretch')

            st.write("### Client Filing Matrix")
            def build_matrix_val(row):
                base_str = row['status']
                assignee = row['assigned_to'] if pd.notnull(row['assigned_to']) and row['assigned_to'] != "" else "Unassigned"
                if row['status'] not in ['Return Filed', 'NA']:
                    due_date = local_calc_due_date(row['period'], row['return_type'], row['filing_frequency'])
                    if due_date and today > due_date: base_str += " 🚨 OVERDUE"
                if row['client_status'] == 'Left': base_str += " [LEFT]"
                return f"{base_str} 👤 {assignee}"

            df['Display Name'] = df['trade_name'].astype(str) + " (" + df['gstin'].astype(str) + ") [" + df['filing_frequency'].astype(str) + "]"
            df['matrix_val'] = df.apply(build_matrix_val, axis=1)
            
            pivot_df = df.pivot_table(index='Display Name', columns='return_type', values='matrix_val', aggfunc='first').fillna('NA')
            for r in active_returns:
                if r not in pivot_df.columns: pivot_df[r] = 'NA'
            
            st.dataframe(pivot_df[active_returns].style.map(color_status), width='stretch', height=500)
            
            df_comments = df[df['comments'].str.len() > 0][['trade_name', 'return_type', 'status', 'assigned_to', 'comments']]
            if not df_comments.empty:
                st.write("### 💬 Active Comments")
                st.dataframe(df_comments, width='stretch')
        else:
            if not is_initialized: st.warning(f"Filings for {selected_period} have not been initialized yet.")
            else: st.info("No matching records found for the applied filters.")

        if st.session_state.rights['can_manage_clients']:
            st.markdown("---")
            if not is_initialized:
                if st.button(f"🚀 Initialize {selected_period} Tasks for {selected_org}"):
                    current_idx = PERIOD_OPTIONS.index(selected_period) if selected_period in PERIOD_OPTIONS else -1
                    prev_period = PERIOD_OPTIONS[current_idx + 1] if current_idx >= 0 and current_idx + 1 < len(PERIOD_OPTIONS) else None
                    p_date_start = datetime.datetime.strptime(selected_period, "%B %Y").date()
                    
                    clients = c.execute("SELECT gstin, applicable_returns, client_status, left_wef FROM clients WHERE org_name=?", (selected_org,)).fetchall()
                    for client in clients:
                        gstin, app_returns_str, c_status, c_wef = client
                        if c_status == 'Left' and c_wef:
                            try:
                                wef_date = datetime.datetime.strptime(c_wef, "%Y-%m-%d").date()
                                if wef_date < p_date_start: continue
                            except: pass
                        
                        app_returns_str = app_returns_str if app_returns_str else "GSTR1,GSTR3B"
                        r_types = [r.strip() for r in app_returns_str.split(',') if r.strip()]
                        
                        for r_type in r_types:
                            c.execute(f"SELECT 1 FROM filings WHERE gstin='{gstin}' AND return_type='{r_type}' AND period='{selected_period}'")
                            if not c.fetchone():
                                prev_note_str = ""
                                if prev_period:
                                    c.execute(f"SELECT carry_forward_note FROM filings WHERE gstin='{gstin}' AND return_type='{r_type}' AND period='{prev_period}'")
                                    p_note = c.fetchone()
                                    if p_note and p_note[0]: prev_note_str = f"📌 [NOTE FROM {prev_period}]: {p_note[0]} "
                                c.execute("INSERT INTO filings (gstin, return_type, period, status, assigned_to, comments, carry_forward_note) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                                          (gstin, r_type, selected_period, 'Pending', 'Unassigned', prev_note_str, ''))
                    conn.commit()
                    log_audit(st.session_state.username, "Initialize Month", f"Generated {selected_period} tasks for {selected_org}.")
                    st.rerun()
            else:
                st.success(f"✅ Tasks for {selected_period} are actively initialized.")
                if st.button("🔄 Sync Missing Clients/Returns"):
                    p_date_start = datetime.datetime.strptime(selected_period, "%B %Y").date()
                    clients = c.execute("SELECT gstin, applicable_returns, client_status, left_wef FROM clients WHERE org_name=?", (selected_org,)).fetchall()
                    for client in clients:
                        gstin, app_returns_str, c_status, c_wef = client
                        if c_status == 'Left' and c_wef:
                            try:
                                wef_date = datetime.datetime.strptime(c_wef, "%Y-%m-%d").date()
                                if wef_date < p_date_start: continue
                            except: pass
                            
                        app_returns_str = app_returns_str if app_returns_str else "GSTR1,GSTR3B"
                        r_types = [r.strip() for r in app_returns_str.split(',') if r.strip()]
                        for r_type in r_types:
                            c.execute(f"INSERT INTO filings (gstin, return_type, period, status, assigned_to, comments, carry_forward_note) SELECT '{gstin}', '{r_type}', '{selected_period}', 'Pending', 'Unassigned', '', '' WHERE NOT EXISTS (SELECT 1 FROM filings WHERE gstin='{gstin}' AND return_type='{r_type}' AND period='{selected_period}')")
                    conn.commit()
                    st.toast("Client lists synced for this month!")
                    st.rerun()

    # --- B. Update Work ---
    elif choice == "Update Work":
        st.title(f"Manage Filing Status ({selected_period})")
        st.caption(f"Filtering records for Organization: **{selected_org}**")
        
        st.markdown("##### 🔍 Task Filters")
        task_search = st.text_input("Type to Filter (Searches Trade Name, GSTIN, and Comments)")
        
        col_f1, col_f2, col_f3, col_f4 = st.columns(4)
        all_users = [row[0] for row in c.execute("SELECT username FROM users").fetchall()]
        
        filter_return = col_f1.multiselect("Return Types (Leave blank for All)", all_master_returns)
        filter_status = col_f2.multiselect("Statuses (Leave blank for All)", STATUS_OPTIONS)
        filter_status_exclude = col_f2.checkbox("Exclude selected statuses")
        filter_user = col_f3.multiselect("Assigned To (Leave blank for All)", ["Unassigned"] + all_users)
        filter_freq = col_f4.multiselect("Frequencies (Leave blank for All)", all_freqs)

        query = """SELECT f.id, c.trade_name, c.filing_frequency, f.period, f.return_type, f.status, f.assigned_to, f.comments, f.carry_forward_note 
                   FROM filings f JOIN clients c ON f.gstin = c.gstin 
                   WHERE f.period = ? AND c.org_name = ?"""
        df_tasks = pd.read_sql_query(query, conn, params=(selected_period, selected_org))
        
        if not df_tasks.empty:
            if task_search:
                df_tasks = df_tasks[df_tasks['trade_name'].str.contains(task_search, case=False, na=False) | 
                                    df_tasks['comments'].str.contains(task_search, case=False, na=False)]
            if filter_return: df_tasks = df_tasks[df_tasks['return_type'].isin(filter_return)]
            if filter_freq: df_tasks = df_tasks[df_tasks['filing_frequency'].isin(filter_freq)]
            if filter_status:
                if filter_status_exclude: df_tasks = df_tasks[~df_tasks['status'].isin(filter_status)]
                else: df_tasks = df_tasks[df_tasks['status'].isin(filter_status)]
            if filter_user:
                assigned_mask = df_tasks['assigned_to'].isin(filter_user)
                if "Unassigned" in filter_user:
                    assigned_mask = assigned_mask | (df_tasks['assigned_to'] == '') | (df_tasks['assigned_to'].isna()) | (df_tasks['assigned_to'] == 'Unassigned')
                df_tasks = df_tasks[assigned_mask]
        
        if not df_tasks.empty:
            df_tasks['Due Date'] = df_tasks.apply(lambda row: local_calc_due_date(row['period'], row['return_type'], row['filing_frequency']), axis=1)
            st.dataframe(df_tasks[['trade_name', 'filing_frequency', 'return_type', 'Due Date', 'status', 'assigned_to', 'comments', 'carry_forward_note']], width='stretch')
            st.markdown("---")
            
            task_options = df_tasks.apply(lambda r: f"{r['trade_name']} | {r['return_type']} | [{r['status']}]", axis=1)
            selected_task_str = st.selectbox("Select Client & Return to Update", task_options)
            client_name, return_type, current_status_bracket = selected_task_str.split(" | ")
            current_status = current_status_bracket.strip("[]")
            
            row_data = df_tasks[(df_tasks['trade_name'] == client_name) & (df_tasks['return_type'] == return_type)].iloc[0]
            existing_comment = row_data['comments'] or ""
            existing_cf_note = row_data['carry_forward_note'] or ""
            existing_assignee = row_data['assigned_to'] or "Unassigned"
            client_freq = row_data['filing_frequency']
            
            if st.session_state.rights['can_assign_others']: assignee_options = ["Unassigned"] + all_users
            else:
                assignee_options = ["Unassigned", st.session_state.username]
                if existing_assignee not in assignee_options: assignee_options.append(existing_assignee)

            is_super_admin = (st.session_state.rights['role'] == 'Super Admin')
            c.execute("SELECT value FROM settings WHERE key = 'global_lock_date'")
            lock_res = c.fetchone()
            is_locked_by_global = False
            if lock_res and lock_res[0]:
                try:
                    global_lock_date = datetime.datetime.strptime(lock_res[0], "%Y-%m-%d").date()
                    if today >= global_lock_date: is_locked_by_global = True
                except: pass
                
            is_locked_by_auto = False
            task_due_date = local_calc_due_date(selected_period, return_type, client_freq)
            auto_lock_days = returns_lock_db.get(return_type, -1)
            if auto_lock_days >= 0 and task_due_date:
                lock_trigger_date = task_due_date + datetime.timedelta(days=auto_lock_days)
                if today > lock_trigger_date: is_locked_by_auto = True
            
            is_locked_by_status = (current_status == "Return Filed")
            
            if (is_locked_by_status or is_locked_by_global or is_locked_by_auto) and not is_super_admin:
                if is_locked_by_auto: st.error(f"🔒 **Auto-Locked:** The '{return_type}' master rule automatically locks records {auto_lock_days} days after the due date.")
                elif is_locked_by_global: st.error("🔒 **System Locked:** The global lockout date has passed.")
                else: st.error("🔒 **Status Locked:** This return is marked as 'Return Filed'. Only a Super Admin can edit it.")
            else:
                with st.form("update_task_form"):
                    col1, col2 = st.columns(2)
                    with col1: new_status = st.selectbox("Update Status To", STATUS_OPTIONS, index=STATUS_OPTIONS.index(current_status) if current_status in STATUS_OPTIONS else 0)
                    with col2: assignee = st.selectbox("Assign To", assignee_options, index=assignee_options.index(existing_assignee) if existing_assignee in assignee_options else 0)
                    new_comments = st.text_area("Current Remarks / Comments", value=existing_comment)
                    st.markdown("##### ⏭️ Prep for Next Month")
                    new_cf_note = st.text_input("Add a note that will automatically carry forward to next month's task:", value=existing_cf_note)
                    
                    if st.form_submit_button("Save Changes"):
                        before_str = f"Status: {current_status}, Assigned To: {existing_assignee}, CF Note: '{existing_cf_note}'"
                        after_str = f"Status: {new_status}, Assigned To: {assignee}, CF Note: '{new_cf_note}'"
                        c.execute("""UPDATE filings SET status = ?, assigned_to = ?, comments = ?, carry_forward_note = ? WHERE return_type = ? AND period = ? AND gstin = (SELECT gstin FROM clients WHERE trade_name = ?)""", 
                                  (new_status, assignee, new_comments, new_cf_note, return_type, selected_period, client_name))
                        conn.commit()
                        log_audit(st.session_state.username, "Update Task", f"Updated {client_name} {return_type} ({selected_period})", before_state=before_str, after_state=after_str)
                        st.success(f"Successfully updated {client_name}!")
                        st.rerun()
        else:
            st.info(f"No tasks found for {selected_period} matching your filters.")

    # --- C. Admin Settings ---
    elif choice == "Admin Settings":
        st.title("System & Security Settings")
        # UPGRADE: Add Organizations tab
        tab_org, tab_users, tab_roles, tab_returns, tab_freq, tab_sec, tab_fy, tab_backup = st.tabs([
            "Organizations", "Users", "Roles (RBAC)", "Returns Master", "Frequencies Master", "Security Locks", "Financial Years", "Data Backup"
        ])
        
        # UPGRADE: Organization Master with Rename Support
        with tab_org:
            st.subheader("Organizations Master")
            st.dataframe(pd.read_sql_query("SELECT org_name AS 'Registered Organizations' FROM organizations", conn), width='stretch')
            col_o1, col_o2, col_o3 = st.columns(3)
            
            with col_o1:
                st.markdown("**Add New Organization**")
                with st.form("add_org_form"):
                    new_org = st.text_input("New Organization Name")
                    if st.form_submit_button("Add Organization") and new_org:
                        try:
                            c.execute("INSERT INTO organizations VALUES (?)", (new_org.strip(),))
                            conn.commit()
                            log_audit(st.session_state.username, "Add Org", f"Created organization: {new_org}")
                            st.success(f"Organization '{new_org}' added!")
                            st.rerun()
                        except sqlite3.IntegrityError: st.error("Organization already exists.")
                        
            with col_o2:
                st.markdown("**Rename Organization**")
                with st.form("rename_org_form"):
                    old_org_name = st.selectbox("Select Organization to Rename", all_orgs)
                    renamed_org = st.text_input("New Name")
                    if st.form_submit_button("Rename Organization") and renamed_org:
                        if renamed_org.strip() in all_orgs:
                            st.error("An organization with this name already exists.")
                        else:
                            c.execute("UPDATE organizations SET org_name = ? WHERE org_name = ?", (renamed_org.strip(), old_org_name))
                            c.execute("UPDATE clients SET org_name = ? WHERE org_name = ?", (renamed_org.strip(), old_org_name))
                            conn.commit()
                            log_audit(st.session_state.username, "Rename Org", f"Renamed {old_org_name} to {renamed_org}")
                            st.success(f"Renamed to '{renamed_org}' successfully!")
                            st.rerun()
                            
            with col_o3:
                st.markdown("**Delete Organization**")
                with st.form("del_org_form"):
                    del_org = st.selectbox("Remove Organization", all_orgs)
                    if st.form_submit_button("Delete Organization", type="primary"):
                        c.execute("SELECT COUNT(*) FROM clients WHERE org_name = ?", (del_org,))
                        if c.fetchone()[0] > 0:
                            st.error("Cannot delete an organization that has active clients.")
                        elif len(all_orgs) <= 1:
                            st.error("You must have at least one organization in the system.")
                        else:
                            c.execute("DELETE FROM organizations WHERE org_name = ?", (del_org,))
                            conn.commit()
                            log_audit(st.session_state.username, "Delete Org", f"Deleted organization: {del_org}")
                            st.success(f"Organization '{del_org}' removed.")
                            st.rerun()

        with tab_users:
            st.dataframe(pd.read_sql_query("SELECT username, role, last_login FROM users", conn), width='stretch')
            all_roles = [row[0] for row in c.execute("SELECT role_name FROM roles").fetchall()]
            all_users = [row[0] for row in c.execute("SELECT username FROM users").fetchall()]
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Add New User**")
                with st.form("add_user_form"):
                    new_user = st.text_input("New Username")
                    new_pass = st.text_input("Password")
                    new_role = st.selectbox("Assign Role", all_roles)
                    if st.form_submit_button("Create User") and new_user and new_pass:
                        try:
                            c.execute("INSERT INTO users (username, password, role, session_token, last_login) VALUES (?, ?, ?, '', '')", (new_user, new_pass, new_role))
                            conn.commit()
                            log_audit(st.session_state.username, "Add User", f"Created account for {new_user}", after_state=f"Role: {new_role}")
                            st.success(f"User {new_user} added successfully!")
                            st.rerun()
                        except sqlite3.IntegrityError: st.error("Username already exists.")
            with col2:
                st.markdown("**Edit or Delete User**")
                edit_user = st.selectbox("Select User to Modify", all_users)
                with st.form("edit_user_form"):
                    updated_pass = st.text_input("New Password (leave blank to keep current)")
                    updated_role = st.selectbox("Change Role", all_roles)
                    sub_col1, sub_col2 = st.columns(2)
                    if sub_col1.form_submit_button("Update User"):
                        old_role = c.execute("SELECT role FROM users WHERE username=?", (edit_user,)).fetchone()[0]
                        if updated_pass: c.execute("UPDATE users SET password = ?, role = ? WHERE username = ?", (updated_pass, updated_role, edit_user))
                        else: c.execute("UPDATE users SET role = ? WHERE username = ?", (updated_role, edit_user))
                        c.execute("UPDATE users SET session_token = '' WHERE username = ?", (edit_user,))
                        conn.commit()
                        log_audit(st.session_state.username, "Edit User", f"Updated details for {edit_user}", before_state=f"Role: {old_role}", after_state=f"Role: {updated_role}")
                        st.success(f"User {edit_user} updated! They will be forced to log in again.")
                        st.rerun()
                    if sub_col2.form_submit_button("Delete User", type="primary"):
                        if edit_user == st.session_state.username: st.error("You cannot delete yourself.")
                        else:
                            old_role = c.execute("SELECT role FROM users WHERE username=?", (edit_user,)).fetchone()[0]
                            c.execute("DELETE FROM users WHERE username = ?", (edit_user,))
                            conn.commit()
                            log_audit(st.session_state.username, "Delete User", f"Deleted account {edit_user}", before_state=f"Role: {old_role}")
                            st.success(f"User {edit_user} deleted.")
                            st.rerun()

        with tab_roles:
            st.dataframe(pd.read_sql_query("SELECT * FROM roles", conn), width='stretch')
            col_r1, col_r2 = st.columns(2)
            with col_r1:
                st.markdown("**Create New Role**")
                with st.form("add_role_form"):
                    r_name = st.text_input("Role Name")
                    c_client = st.checkbox("Can Add/Edit/Delete Clients")
                    c_user = st.checkbox("Can Access Admin Settings")
                    c_audit = st.checkbox("Can View Audit Logs")
                    c_assign = st.checkbox("Can Assign Work to Other Users")
                    if st.form_submit_button("Create Role") and r_name:
                        try:
                            c.execute("INSERT INTO roles VALUES (?, ?, ?, ?, ?)", (r_name, int(c_client), int(c_user), int(c_audit), int(c_assign)))
                            conn.commit()
                            log_audit(st.session_state.username, "Add Role", f"Created role {r_name}")
                            st.success(f"Role {r_name} created!")
                            st.rerun()
                        except sqlite3.IntegrityError: st.error("Role name already exists.")
            with col_r2:
                st.markdown("**Delete a Role**")
                del_role = st.selectbox("Select Role to Delete", [r for r in all_roles if r != "Super Admin"])
                if st.button("Delete Selected Role"):
                    c.execute("SELECT COUNT(*) FROM users WHERE role = ?", (del_role,))
                    if c.fetchone()[0] > 0: st.error("Cannot delete. Change users assigned to this role first.")
                    else:
                        c.execute("DELETE FROM roles WHERE role_name = ?", (del_role,))
                        conn.commit()
                        log_audit(st.session_state.username, "Delete Role", f"Deleted role {del_role}")
                        st.success(f"Role {del_role} deleted.")
                        st.rerun()

        with tab_returns:
            st.subheader("Compliance & Returns Master")
            df_returns = pd.read_sql_query("SELECT * FROM returns_master", conn)
            df_dates = pd.read_sql_query("SELECT return_name, freq_name, due_month_type || ' ' || due_day AS due_timing FROM return_due_dates", conn)
            if not df_dates.empty:
                pivot_dates = df_dates.pivot(index='return_name', columns='freq_name', values='due_timing')
                display_df = df_returns.merge(pivot_dates, on='return_name', how='left')
            else: display_df = df_returns
            st.dataframe(display_df, width='stretch')
            
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                st.markdown("**Add / Edit Return Configuration**")
                existing_returns = ["<Create New>"] + all_master_returns
                ret_action = st.selectbox("Select Return to Edit, or Create New", existing_returns)
                
                if ret_action == "<Create New>":
                    edit_ret_name = ""
                    edit_lock = -1
                    edit_freqs = []
                else:
                    edit_ret_name = ret_action
                    c.execute("SELECT auto_lock_days FROM returns_master WHERE return_name=?", (edit_ret_name,))
                    edit_lock = c.fetchone()[0]
                    c.execute("SELECT freq_name FROM return_due_dates WHERE return_name=?", (edit_ret_name,))
                    edit_freqs = [row[0] for row in c.fetchall()]
                    
                final_ret_name = st.text_input("Return Name (e.g., GSTR9)", value=edit_ret_name, disabled=(ret_action != "<Create New>"))
                final_lock = st.number_input("Auto-Lock (Days after due date. -1 to disable)", value=edit_lock)
                final_freqs = st.multiselect("Applicable Frequencies for this Return", all_freqs, default=[f for f in edit_freqs if f in all_freqs])
                
                freq_configs = {}
                month_options = ['Next Month', 'Same Month'] + list(calendar.month_name)[1:]
                
                if final_freqs:
                    st.markdown("##### 📅 Due Dates (Offset Configuration)")
                    for f in final_freqs:
                        st.markdown(f"**{f} Rules**")
                        c1, c2 = st.columns(2)
                        curr_day = 20
                        curr_m_type = 'Next Month'
                        if ret_action != "<Create New>":
                            c.execute("SELECT due_day, due_month_type FROM return_due_dates WHERE return_name=? AND freq_name=?", (edit_ret_name, f))
                            res = c.fetchone()
                            if res: 
                                curr_day, curr_m_type = res[0], res[1]
                        with c1: selected_m_type = st.selectbox(f"Timing Month", month_options, index=month_options.index(curr_m_type) if curr_m_type in month_options else 0, key=f"mt_{f}")
                        with c2: selected_day = st.number_input(f"Day of Month", min_value=1, max_value=31, value=curr_day, key=f"dd_{f}")
                        freq_configs[f] = {'day': selected_day, 'month_type': selected_m_type}
                        
                if st.button("Save Compliance Rule", type="primary"):
                    target_name = final_ret_name.upper().strip() if ret_action == "<Create New>" else edit_ret_name
                    if target_name and final_freqs:
                        c.execute("INSERT OR REPLACE INTO returns_master VALUES (?, ?)", (target_name, final_lock))
                        c.execute("DELETE FROM return_due_dates WHERE return_name=?", (target_name,))
                        for f_name, config in freq_configs.items():
                            c.execute("INSERT INTO return_due_dates VALUES (?, ?, ?, ?)", (target_name, f_name, config['day'], config['month_type']))
                        conn.commit()
                        log_audit(st.session_state.username, "Update Returns Master", f"Updated mapping for {target_name}")
                        st.success(f"Rule for {target_name} saved!")
                        st.rerun()
                    else: st.error("Please provide a Return Name and select at least one Applicable Frequency.")

            with col_m2:
                st.markdown("**Delete Return Rule**")
                del_ret = st.selectbox("Select Return Type to Delete", all_master_returns)
                if st.button("Delete Return Type"):
                    c.execute("DELETE FROM returns_master WHERE return_name = ?", (del_ret,))
                    c.execute("DELETE FROM return_due_dates WHERE return_name = ?", (del_ret,))
                    conn.commit()
                    log_audit(st.session_state.username, "Delete Return Rule", f"Deleted rule for {del_ret}")
                    st.success(f"Return rule {del_ret} deleted.")
                    st.rerun()

        with tab_freq:
            st.subheader("Frequency Master")
            st.dataframe(pd.read_sql_query("SELECT freq_name AS 'Available Frequencies' FROM frequency_master", conn), width='stretch')
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                with st.form("add_freq_form"):
                    new_freq = st.text_input("Add New Frequency (e.g., Bi-Monthly)")
                    if st.form_submit_button("Add Frequency") and new_freq:
                        try:
                            c.execute("INSERT INTO frequency_master VALUES (?)", (new_freq.strip(),))
                            conn.commit()
                            log_audit(st.session_state.username, "Add Frequency", f"Added {new_freq}")
                            st.success(f"Frequency '{new_freq}' added!")
                            st.rerun()
                        except sqlite3.IntegrityError: st.error("Frequency already exists.")
            with col_f2:
                with st.form("del_freq_form"):
                    del_freq = st.selectbox("Remove Frequency", all_freqs)
                    if st.form_submit_button("Delete Frequency"):
                        c.execute("DELETE FROM frequency_master WHERE freq_name = ?", (del_freq,))
                        c.execute("DELETE FROM return_due_dates WHERE freq_name = ?", (del_freq,))
                        conn.commit()
                        log_audit(st.session_state.username, "Delete Frequency", f"Deleted {del_freq}")
                        st.success(f"Frequency '{del_freq}' removed.")
                        st.rerun()

        with tab_sec:
            st.subheader("Global Data Locking")
            c.execute("SELECT value FROM settings WHERE key = 'global_lock_date'")
            curr_lock_str = c.fetchone()[0]
            try: default_lock_date = datetime.datetime.strptime(curr_lock_str, "%Y-%m-%d").date()
            except: default_lock_date = today
            with st.form("lock_date_form"):
                new_lock_date = st.date_input("Select Lockout Date", value=default_lock_date)
                if st.form_submit_button("Set Global Lock"):
                    c.execute("UPDATE settings SET value = ? WHERE key = 'global_lock_date'", (new_lock_date.strftime("%Y-%m-%d"),))
                    conn.commit()
                    log_audit(st.session_state.username, "Security Update", "Changed global lockout date.", before_state=curr_lock_str, after_state=new_lock_date.strftime("%Y-%m-%d"))
                    st.rerun()

        with tab_fy:
            st.subheader("Financial Years")
            latest_fy = max(available_fys)
            start_yr = int(latest_fy.split("-")[0])
            next_fy_prediction = f"{start_yr+1}-{start_yr+2}"
            if st.button(f"Unlock Next FY ({next_fy_prediction})"):
                try:
                    c.execute("INSERT INTO financial_years (fy_string) VALUES (?)", (next_fy_prediction,))
                    conn.commit()
                    log_audit(st.session_state.username, "Add FY", f"Added Financial Year {next_fy_prediction}")
                    st.rerun()
                except sqlite3.IntegrityError: st.error("This FY already exists.")
                
        # UPGRADE: Bug-Free File-Based Data Backup Engine
        with tab_backup:
            st.subheader("System Data Backup")
            st.write("Export your entire system database for safekeeping. Excel format requires `pip install xlsxwriter`.")
            
            col_b1, col_b2 = st.columns(2)
            
            with col_b1:
                st.markdown("**Excel Archive Backup**")
                excel_filename = f"GST_Portal_Backup_{today.strftime('%Y%m%d')}.xlsx"
                try:
                    # Write directly to disk to completely avoid Streamlit BytesIO memory flushing
                    with pd.ExcelWriter(excel_filename, engine='xlsxwriter') as writer:
                        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                        for table in tables:
                            df_dump = pd.read_sql_query(f"SELECT * FROM {table[0]}", conn)
                            df_dump.to_excel(writer, sheet_name=table[0][:31], index=False)
                    
                    with open(excel_filename, "rb") as f:
                        excel_bytes = f.read()
                        
                    st.download_button(label="📊 Download Excel Archive", 
                                       data=excel_bytes, 
                                       file_name=excel_filename, 
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                except ModuleNotFoundError:
                    st.error("Excel generation requires `xlsxwriter`. Open your terminal and run: `pip install xlsxwriter`")
                except Exception as e:
                    st.error(f"Failed to generate Excel: {e}")
            
            with col_b2:
                st.markdown("**Raw SQL Database Backup**")
                with open(DB_NAME, "rb") as f:
                    db_bytes = f.read()
                st.download_button(label="🗄️ Download Raw Database (.db)", 
                                   data=db_bytes, 
                                   file_name=f"GST_Portal_Backup_{today.strftime('%Y%m%d')}.db", 
                                   mime="application/octet-stream")

    # --- D. Clients Master ---
    elif choice == "Clients Master":
        st.title("Comprehensive Client Master")
        st.caption(f"Managing clients for Organization: **{selected_org}**")
        tab1, tab2, tab3, tab4 = st.tabs(["Client List", "Add Single Client", "Bulk Import CSV", "Edit Client Profile"])
        
        with tab1:
            df_clients = pd.read_sql_query("SELECT * FROM clients WHERE org_name=?", conn, params=(selected_org,))
            if 'gst_password' in df_clients.columns: df_clients['gst_password'] = '********'
            st.dataframe(df_clients, width='stretch')
            if not df_clients.empty:
                del_client_str = st.selectbox("Select Client to Remove", df_clients.apply(lambda r: f"{r['trade_name']} ({r['gstin']})", axis=1).tolist())
                if st.button("Remove Client"):
                    del_gstin = del_client_str.split("(")[-1].strip(")")
                    c.execute("SELECT * FROM clients WHERE gstin=?", (del_gstin,))
                    old_client_data = str(c.fetchone())
                    c.execute("DELETE FROM clients WHERE gstin = ?", (del_gstin,))
                    conn.commit()
                    log_audit(st.session_state.username, "Delete Client", f"Removed client {del_gstin}", before_state=old_client_data)
                    st.success("Client removed.")
                    st.rerun()

        with tab2:
            with st.form("add_client_form"):
                col1, col2 = st.columns(2)
                with col1:
                    gstin = st.text_input("GSTIN *")
                    trade_name = st.text_input("Trade Name *")
                    legal_name = st.text_input("Legal Name")
                    business_type = st.selectbox("Constitution", ["Proprietorship", "Partnership", "Private Limited", "LLP", "HUF", "Other"])
                with col2:
                    filing_freq = st.selectbox("Filing Frequency *", all_freqs)
                    state = st.text_input("State/Jurisdiction")
                    contact_person = st.text_input("Contact Person")
                    contact_number = st.text_input("Contact Number")

                st.markdown("##### 📌 Compliance Mapping")
                app_ret = st.multiselect("Applicable Returns", all_master_returns, default=["GSTR1", "GSTR3B"] if "GSTR1" in all_master_returns else None)
                
                col3, col4 = st.columns(2)
                with col3: gst_user = st.text_input("GST Portal Username")
                with col4: gst_pass = st.text_input("GST Portal Password", type="password")
                
                st.markdown("##### 🔴 Client Status")
                c_stat1, c_stat2 = st.columns(2)
                with c_stat1: upd_status = st.selectbox("Status", ["Active", "Left"])
                with c_stat2: 
                    upd_wef = st.date_input("Left W.E.F (With Effect From)", disabled=(upd_status == 'Active'))
                    upd_wef_str = upd_wef.strftime("%Y-%m-%d") if upd_status == 'Left' else ""

                custom_notes = st.text_area("Custom/Additional Details")
                
                if st.form_submit_button("Save Client Profile") and gstin and trade_name:
                    app_ret_str = ",".join(app_ret)
                    try:
                        c.execute("""INSERT INTO clients (gstin, org_name, trade_name, legal_name, filing_frequency, business_type, state, gst_username, gst_password, contact_person, contact_number, custom_data, applicable_returns, client_status, left_wef) 
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                                  (gstin, selected_org, trade_name, legal_name, filing_freq, business_type, state, gst_user, gst_pass, contact_person, contact_number, custom_notes, app_ret_str, upd_status, upd_wef_str))
                        conn.commit()
                        log_audit(st.session_state.username, "Add Client", f"Added {trade_name} to {selected_org}", after_state=f"Returns: {app_ret_str}")
                        st.success("Client profile securely saved!")
                    except sqlite3.IntegrityError: st.error("This GSTIN is already registered in the system.")

        with tab3:
            csv_template = "gstin,trade_name,legal_name,filing_frequency,business_type,state,gst_username,gst_password,contact_person,contact_number,applicable_returns,client_status,left_wef\n"
            csv_template += "27AAACA1234A1Z1,Demo Enterprises,Demo Pvt Ltd,Monthly,Private Limited,Maharashtra,demo_gst,Pass@123,John Doe,9876543210,\"GSTR1,GSTR3B\",Active,\n"
            st.download_button(label="📥 Download Sample Import Template", data=csv_template, file_name="client_master_template.csv", mime="text/csv")
            uploaded_file = st.file_uploader(f"Upload CSV (Importing to {selected_org})", type=['csv'])
            if uploaded_file is not None:
                df_import = pd.read_csv(uploaded_file)
                if 'gstin' in df_import.columns and 'trade_name' in df_import.columns:
                    standard_cols = ['gstin', 'trade_name', 'legal_name', 'filing_frequency', 'business_type', 'state', 'gst_username', 'gst_password', 'contact_person', 'contact_number', 'applicable_returns', 'client_status', 'left_wef']
                    skipped_records = []
                    for _, row in df_import.iterrows():
                        data = {col: str(row[col]) if col in row else "" for col in standard_cols}
                        c_stat = data.get('client_status', 'Active') if data.get('client_status') else 'Active'
                        c_wef = data.get('left_wef', '')
                        
                        extra_cols = row.drop(labels=[col for col in standard_cols if col in row.index])
                        try:
                            c.execute("""INSERT INTO clients (gstin, org_name, trade_name, legal_name, filing_frequency, business_type, state, gst_username, gst_password, contact_person, contact_number, custom_data, applicable_returns, client_status, left_wef) 
                                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                                      (data['gstin'], selected_org, data['trade_name'], data['legal_name'], data['filing_frequency'], data['business_type'], data['state'], data['gst_username'], data['gst_password'], data['contact_person'], data['contact_number'], extra_cols.to_json(), data['applicable_returns'], c_stat, c_wef))
                        except sqlite3.IntegrityError:
                            skipped_records.append(data['gstin'])
                    conn.commit()
                    log_audit(st.session_state.username, "Bulk Import", f"Imported CSV. Skipped {len(skipped_records)} duplicates.")
                    if skipped_records:
                        sample_skips = ", ".join(skipped_records[:5]) + ("..." if len(skipped_records) > 5 else "")
                        st.warning(f"⚠️ Import completed, but {len(skipped_records)} records were skipped because the GSTIN already exists. Skipped: {sample_skips}")
                    else: st.success("✅ Import complete! All client profiles added successfully.")
                else: st.error("CSV must contain at least 'gstin' and 'trade_name' headers.")

        with tab4:
            df_clients_edit = pd.read_sql_query("SELECT * FROM clients WHERE org_name=?", conn, params=(selected_org,))
            if not df_clients_edit.empty:
                edit_client_str = st.selectbox("Select Client to Edit", df_clients_edit.apply(lambda r: f"{r['trade_name']} ({r['gstin']})", axis=1).tolist())
                if edit_client_str:
                    edit_gstin = edit_client_str.split("(")[-1].strip(")")
                    c.execute("SELECT * FROM clients WHERE gstin = ?", (edit_gstin,))
                    c_data = c.fetchone()
                    if c_data:
                        ex_gstin, ex_org, ex_trade_name, ex_legal_name, ex_freq, ex_type, ex_state, ex_gst_user, ex_gst_pass, ex_person, ex_number, ex_custom, ex_app_ret, ex_status, ex_wef = c_data
                        b_types = ["Proprietorship", "Partnership", "Private Limited", "LLP", "HUF", "Other"]
                        ex_ret_list = [r.strip() for r in str(ex_app_ret).split(',')] if ex_app_ret else ["GSTR1", "GSTR3B"]
                        
                        with st.form("edit_client_form"):
                            st.info(f"Editing Details for: {ex_trade_name}")
                            col1, col2 = st.columns(2)
                            with col1:
                                upd_gstin = st.text_input("GSTIN *", value=ex_gstin, disabled=True) 
                                upd_trade_name = st.text_input("Trade Name *", value=ex_trade_name)
                                upd_legal_name = st.text_input("Legal Name", value=ex_legal_name)
                                upd_type = st.selectbox("Constitution", b_types, index=b_types.index(ex_type) if ex_type in b_types else 0)
                            with col2:
                                upd_freq = st.selectbox("Filing Frequency *", all_freqs, index=all_freqs.index(ex_freq) if ex_freq in all_freqs else 0)
                                upd_state = st.text_input("State/Jurisdiction", value=ex_state)
                                upd_person = st.text_input("Contact Person", value=ex_person)
                                upd_number = st.text_input("Contact Number", value=ex_number)

                            st.markdown("##### 📌 Compliance Mapping")
                            safe_default_ret = [r for r in ex_ret_list if r in all_master_returns]
                            upd_app_ret = st.multiselect("Applicable Returns", all_master_returns, default=safe_default_ret)

                            col3, col4 = st.columns(2)
                            with col3: upd_gst_user = st.text_input("GST Portal Username", value=ex_gst_user)
                            with col4: upd_gst_pass = st.text_input("GST Portal Password", value=ex_gst_pass, type="password")
                            
                            st.markdown("##### 🔴 Client Status")
                            c_stat1, c_stat2 = st.columns(2)
                            with c_stat1: 
                                upd_status = st.selectbox("Status", ["Active", "Left"], index=0 if ex_status == 'Active' else 1)
                            with c_stat2: 
                                try: default_wef = datetime.datetime.strptime(ex_wef, "%Y-%m-%d").date() if ex_wef else today
                                except: default_wef = today
                                upd_wef = st.date_input("Left W.E.F", value=default_wef, disabled=(upd_status == 'Active'))
                                upd_wef_str = upd_wef.strftime("%Y-%m-%d") if upd_status == 'Left' else ""

                            upd_custom = st.text_area("Custom/Additional Details", value=ex_custom)
                            
                            if st.form_submit_button("Update Client Profile") and upd_trade_name:
                                upd_app_ret_str = ",".join(upd_app_ret)
                                before_str = f"Freq: {ex_freq}, Status: {ex_status}"
                                after_str = f"Freq: {upd_freq}, Status: {upd_status}"
                                c.execute("""UPDATE clients SET trade_name=?, legal_name=?, filing_frequency=?, business_type=?, state=?, gst_username=?, gst_password=?, contact_person=?, contact_number=?, custom_data=?, applicable_returns=?, client_status=?, left_wef=? WHERE gstin=?""", 
                                          (upd_trade_name, upd_legal_name, upd_freq, upd_type, upd_state, upd_gst_user, upd_gst_pass, upd_person, upd_number, upd_custom, upd_app_ret_str, upd_status, upd_wef_str, ex_gstin))
                                conn.commit()
                                log_audit(st.session_state.username, "Edit Client", f"Updated profile for {upd_trade_name}", before_state=before_str, after_state=after_str)
                                st.success(f"Profile for {upd_trade_name} updated successfully!")
                                st.rerun()

    # --- E. Audit Logs ---
    elif choice == "Audit Logs":
        st.title("System Audit Trail")
        st.dataframe(pd.read_sql_query("SELECT timestamp, username, action, details, before_state, after_state FROM audit_logs ORDER BY id DESC", conn), width='stretch')

    conn.close()

if __name__ == "__main__":
    if not st.session_state.logged_in: login()
    else: main_app()