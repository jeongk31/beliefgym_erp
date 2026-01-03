from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from supabase import create_client, Client
from functools import wraps
from datetime import datetime, timedelta, timezone
import config

# Korean timezone (UTC+9)
KST = timezone(timedelta(hours=9))


def parse_datetime(dt_string):
    """
    Parse ISO datetime string robustly, handling various formats from Supabase.
    Handles cases where microseconds have variable digits (e.g., 5 digits instead of 6).
    """
    if not dt_string:
        return None
    # Replace Z with +00:00 for UTC
    dt_string = dt_string.replace('Z', '+00:00')
    # Try direct parsing first
    try:
        return datetime.fromisoformat(dt_string)
    except ValueError:
        # Handle microseconds with wrong number of digits
        # Split at the decimal point if exists
        if '.' in dt_string:
            base, rest = dt_string.split('.', 1)
            # Find where the timezone starts (+ or -)
            tz_start = -1
            for i, c in enumerate(rest):
                if c in '+-':
                    tz_start = i
                    break
            if tz_start > 0:
                microseconds = rest[:tz_start]
                tz_part = rest[tz_start:]
                # Pad or truncate microseconds to 6 digits
                microseconds = microseconds[:6].ljust(6, '0')
                dt_string = f"{base}.{microseconds}{tz_part}"
            else:
                # No timezone, just fix microseconds
                microseconds = rest[:6].ljust(6, '0')
                dt_string = f"{base}.{microseconds}"
        return datetime.fromisoformat(dt_string)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Initialize Supabase client
supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                return redirect(url_for('login'))
            if session['user']['role'] not in roles:
                flash('접근 권한이 없습니다.', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def get_display_name(member, all_members_for_trainer):
    """
    Returns display name with phone suffix if there are duplicate names for the same trainer.
    Format: "이름 (1234)" where 1234 is last 4 digits of phone
    """
    member_name = member['member_name']
    trainer_id = member['trainer_id']

    # Count members with same name under same trainer
    same_name_members = [m for m in all_members_for_trainer
                         if m['member_name'] == member_name and m['trainer_id'] == trainer_id]

    if len(same_name_members) > 1:
        # Multiple members with same name - add phone suffix
        phone = member.get('phone', '')
        phone_suffix = phone[-4:] if len(phone) >= 4 else phone
        return f"{member_name} ({phone_suffix})"

    return member_name


def add_display_names_to_members(members):
    """
    Adds display_name field to each member in the list.
    Groups by trainer_id to detect duplicates per trainer.
    """
    for member in members:
        member['display_name'] = get_display_name(member, members)
    return members


def deduplicate_members_for_dropdown(members):
    """
    Deduplicates members with the same name + phone (same person with multiple entries).
    Returns only one entry per unique person, keeping the oldest entry.
    This is used for schedule dropdowns where we treat same name+phone as one person.
    """
    # Group by (trainer_id, member_name, phone)
    person_map = {}
    for member in members:
        key = (member.get('trainer_id'), member.get('member_name'), member.get('phone', ''))
        if key not in person_map:
            person_map[key] = member
        else:
            # Keep the one with older created_at if available, otherwise keep first
            existing = person_map[key]
            if member.get('created_at') and existing.get('created_at'):
                if member['created_at'] < existing['created_at']:
                    person_map[key] = member

    return list(person_map.values())


def get_remaining_sessions_for_person(member_name, phone, trainer_id):
    """
    Get total remaining sessions for a person (same name + phone) under a trainer.
    Returns dict with total_remaining, entries (sorted by created_at), and entry with available sessions.
    """
    # Get all member entries with same name, phone, trainer
    response = supabase.table('members').select('id, member_name, phone, sessions, created_at, trainer_id').eq('trainer_id', trainer_id).eq('member_name', member_name).eq('phone', phone).order('created_at').execute()
    entries = response.data if response.data else []

    if not entries:
        return {'total_remaining': 0, 'entries': [], 'available_entry': None}

    # Get completed sessions count for each entry
    entry_ids = [e['id'] for e in entries]
    completed_response = supabase.table('schedules').select('member_id').in_('member_id', entry_ids).eq('status', '수업 완료').execute()
    completed_schedules = completed_response.data if completed_response.data else []

    # Also count planned schedules (not yet completed but scheduled)
    planned_response = supabase.table('schedules').select('member_id').in_('member_id', entry_ids).eq('status', '수업 계획').execute()
    planned_schedules = planned_response.data if planned_response.data else []

    # Count per entry
    completed_counts = {}
    planned_counts = {}
    for s in completed_schedules:
        mid = s['member_id']
        completed_counts[mid] = completed_counts.get(mid, 0) + 1
    for s in planned_schedules:
        mid = s['member_id']
        planned_counts[mid] = planned_counts.get(mid, 0) + 1

    total_remaining = 0
    available_entry = None

    for entry in entries:
        completed = completed_counts.get(entry['id'], 0)
        planned = planned_counts.get(entry['id'], 0)
        used = completed + planned
        remaining = entry['sessions'] - used
        entry['completed_sessions'] = completed
        entry['planned_sessions'] = planned
        entry['remaining_sessions'] = remaining
        total_remaining += max(0, remaining)

        # Find first entry with available sessions (oldest first)
        if available_entry is None and remaining > 0:
            available_entry = entry

    return {
        'total_remaining': total_remaining,
        'entries': entries,
        'available_entry': available_entry
    }


@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        # Query user from database
        response = supabase.table('users').select('*').eq('email', email).execute()

        if response.data and len(response.data) > 0:
            user = response.data[0]
            # Simple password check (in production, use proper hashing)
            if user['password_hash'] == password:
                # Check if user is deactivated
                if user.get('status') == '비활성화':
                    flash('계정이 비활성화되었습니다. 관리자에게 문의하세요.', 'error')
                    return render_template('login.html')

                session['user'] = {
                    'id': user['id'],
                    'name': user['name'],
                    'email': user['email'],
                    'role': user['role'],
                    'branch_id': user['branch_id']
                }
                return redirect(url_for('dashboard'))

        flash('이메일 또는 비밀번호가 올바르지 않습니다.', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    user = session['user']
    today = datetime.now(KST).date()

    # Calculate month ranges
    month_start = today.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    # Previous month range
    if month_start.month == 1:
        prev_month_start = month_start.replace(year=month_start.year - 1, month=12)
    else:
        prev_month_start = month_start.replace(month=month_start.month - 1)

    dashboard_data = {
        'member_count': 0,
        'new_members_this_month': 0,
        'new_members_last_month': 0,
        'sales_this_month': 0,
        'sales_last_month': 0,
        'sessions_today': 0,
        'sessions_completed_today': 0,
        'sessions_this_month': 0,
        'today_schedules': [],
        'recent_members': [],
        'trainer_count': 0,
        'branch_count': 0,
        'top_trainers': [],
        'ot_unassigned': 0,
        'ot_assigned': 0,
        'ot_completed': 0,
        'ot_returned': 0,
    }

    if user['role'] == 'trainer':
        # Trainer dashboard
        trainer_id = user['id']

        # Total members
        members_response = supabase.table('members').select('id, member_name, sessions, unit_price, channel, refund_status, created_at').eq('trainer_id', trainer_id).execute()
        all_members = members_response.data or []
        dashboard_data['member_count'] = len(all_members)

        # New members this month
        new_this_month = [m for m in all_members if m['created_at'][:10] >= month_start.isoformat()]
        dashboard_data['new_members_this_month'] = len(new_this_month)

        # New members last month
        new_last_month = [m for m in all_members if prev_month_start.isoformat() <= m['created_at'][:10] < month_start.isoformat()]
        dashboard_data['new_members_last_month'] = len(new_last_month)

        # Sales this month (50% for WI, refunded members included with proportional amount)
        dashboard_data['sales_this_month'] = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in new_this_month
        )

        # Sales last month
        dashboard_data['sales_last_month'] = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in new_last_month
        )

        # Today's schedules
        schedules_today = supabase.table('schedules').select(
            '*, member:members(member_name)'
        ).eq('trainer_id', trainer_id).eq('schedule_date', today.isoformat()).order('start_time').execute()
        dashboard_data['today_schedules'] = schedules_today.data or []
        dashboard_data['sessions_today'] = len(dashboard_data['today_schedules'])
        dashboard_data['sessions_completed_today'] = len([s for s in dashboard_data['today_schedules'] if s.get('status') == '수업 완료'])

        # Sessions completed this month
        sessions_month = supabase.table('schedules').select('id').eq('trainer_id', trainer_id).eq('status', '수업 완료').gte('schedule_date', month_start.isoformat()).lt('schedule_date', next_month.isoformat()).execute()
        dashboard_data['sessions_this_month'] = len(sessions_month.data or [])

        # Recent members (last 5)
        dashboard_data['recent_members'] = sorted(all_members, key=lambda x: x['created_at'], reverse=True)[:5]

    elif user['role'] == 'branch_admin':
        # Branch admin dashboard
        trainers_response = supabase.table('users').select('id, name').eq('branch_id', user['branch_id']).eq('role', 'trainer').execute()
        trainers = trainers_response.data or []
        trainer_ids = [t['id'] for t in trainers]
        dashboard_data['trainer_count'] = len(trainers)

        if trainer_ids:
            # Total members in branch
            members_response = supabase.table('members').select('id, trainer_id, member_name, sessions, unit_price, channel, refund_status, created_at').in_('trainer_id', trainer_ids).execute()
            all_members = members_response.data or []
            dashboard_data['member_count'] = len(all_members)

            # New members this month
            new_this_month = [m for m in all_members if m['created_at'][:10] >= month_start.isoformat()]
            dashboard_data['new_members_this_month'] = len(new_this_month)
            new_last_month = [m for m in all_members if prev_month_start.isoformat() <= m['created_at'][:10] < month_start.isoformat()]
            dashboard_data['new_members_last_month'] = len(new_last_month)

            # Sales this month (refunded members included with proportional amount)
            dashboard_data['sales_this_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_this_month
            )
            dashboard_data['sales_last_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_last_month
            )

            # Sessions this month
            sessions_month = supabase.table('schedules').select('id').in_('trainer_id', trainer_ids).eq('status', '수업 완료').gte('schedule_date', month_start.isoformat()).lt('schedule_date', next_month.isoformat()).execute()
            dashboard_data['sessions_this_month'] = len(sessions_month.data or [])

            # Top trainers by sales this month
            trainer_sales = {}
            for m in new_this_month:
                tid = m['trainer_id']
                amount = m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                trainer_sales[tid] = trainer_sales.get(tid, 0) + amount

            trainer_name_map = {t['id']: t['name'] for t in trainers}
            top_trainers = sorted(
                [{'id': tid, 'name': trainer_name_map.get(tid, '-'), 'sales': sales} for tid, sales in trainer_sales.items()],
                key=lambda x: x['sales'], reverse=True
            )[:5]
            dashboard_data['top_trainers'] = top_trainers

            # Recent members
            dashboard_data['recent_members'] = sorted(all_members, key=lambda x: x['created_at'], reverse=True)[:5]

        # OT metrics for branch admin
        ot_response = supabase.table('members').select('id, ot_status').eq('member_type', 'OT회원').eq('branch_id', user['branch_id']).execute()
        if ot_response.data:
            for ot in ot_response.data:
                status = ot.get('ot_status', 'unassigned')
                if status == 'unassigned':
                    dashboard_data['ot_unassigned'] += 1
                elif status == 'assigned':
                    dashboard_data['ot_assigned'] += 1
                elif status == 'completed':
                    dashboard_data['ot_completed'] += 1
                elif status == 'returned':
                    dashboard_data['ot_returned'] += 1

    else:  # main_admin
        # Main admin dashboard
        # Get all branches
        branches_response = supabase.table('branches').select('id, name').execute()
        branches = branches_response.data or []
        dashboard_data['branch_count'] = len(branches)

        # Get all trainers
        trainers_response = supabase.table('users').select('id, name, branch_id').eq('role', 'trainer').execute()
        trainers = trainers_response.data or []
        trainer_ids = [t['id'] for t in trainers]
        dashboard_data['trainer_count'] = len(trainers)

        if trainer_ids:
            # All members
            members_response = supabase.table('members').select('id, trainer_id, member_name, sessions, unit_price, channel, refund_status, created_at').execute()
            all_members = members_response.data or []
            dashboard_data['member_count'] = len(all_members)

            # New members this month
            new_this_month = [m for m in all_members if m['created_at'][:10] >= month_start.isoformat()]
            dashboard_data['new_members_this_month'] = len(new_this_month)
            new_last_month = [m for m in all_members if prev_month_start.isoformat() <= m['created_at'][:10] < month_start.isoformat()]
            dashboard_data['new_members_last_month'] = len(new_last_month)

            # Sales this month (refunded members included with proportional amount)
            dashboard_data['sales_this_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_this_month
            )
            dashboard_data['sales_last_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_last_month
            )

            # Sessions this month
            sessions_month = supabase.table('schedules').select('id').eq('status', '수업 완료').gte('schedule_date', month_start.isoformat()).lt('schedule_date', next_month.isoformat()).execute()
            dashboard_data['sessions_this_month'] = len(sessions_month.data or [])

            # Top trainers by sales
            trainer_sales = {}
            for m in new_this_month:
                tid = m['trainer_id']
                amount = m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                trainer_sales[tid] = trainer_sales.get(tid, 0) + amount

            trainer_name_map = {t['id']: t['name'] for t in trainers}
            top_trainers = sorted(
                [{'id': tid, 'name': trainer_name_map.get(tid, '-'), 'sales': sales} for tid, sales in trainer_sales.items()],
                key=lambda x: x['sales'], reverse=True
            )[:5]
            dashboard_data['top_trainers'] = top_trainers

            # Recent members
            dashboard_data['recent_members'] = sorted(all_members, key=lambda x: x['created_at'], reverse=True)[:5]

        # OT metrics for main admin
        ot_response = supabase.table('members').select('id, ot_status').eq('member_type', 'OT회원').execute()
        if ot_response.data:
            for ot in ot_response.data:
                status = ot.get('ot_status', 'unassigned')
                if status == 'unassigned':
                    dashboard_data['ot_unassigned'] += 1
                elif status == 'assigned':
                    dashboard_data['ot_assigned'] += 1
                elif status == 'completed':
                    dashboard_data['ot_completed'] += 1
                elif status == 'returned':
                    dashboard_data['ot_returned'] += 1

    return render_template('dashboard.html', user=user, data=dashboard_data, today=today.isoformat(), current_month=month_start.strftime('%Y년 %m월'))


@app.route('/members')
@login_required
def members():
    user = session['user']

    # Get selected month (default to current month)
    month_str = request.args.get('month')
    if month_str:
        try:
            selected_date = datetime.strptime(month_str, '%Y-%m').date()
        except:
            selected_date = datetime.now(KST).date()
    else:
        selected_date = datetime.now(KST).date()

    # Get filters
    filter_branch_id = request.args.get('branch_id')
    filter_trainer_id = request.args.get('trainer_id')
    filter_trainer_name = None

    # Get branches and trainers for filter dropdowns
    branches_list = []
    trainers_list = []

    # Calculate month range
    month_start = selected_date.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    month_end = next_month - timedelta(days=1)

    # Generate days for the month
    month_days = []
    current = month_start
    while current <= month_end:
        month_days.append({
            'date': current.isoformat(),
            'day': current.day
        })
        current += timedelta(days=1)

    # Get members based on role and filter
    if user['role'] == 'main_admin':
        # Get all branches for filter
        branches_response = supabase.table('branches').select('*').order('name').execute()
        branches_list = branches_response.data if branches_response.data else []

        # Get trainers based on selected branch
        if filter_branch_id:
            trainers_response = supabase.table('users').select('id, name').eq('branch_id', filter_branch_id).eq('role', 'trainer').order('name').execute()
        else:
            trainers_response = supabase.table('users').select('id, name, branch_id').eq('role', 'trainer').order('name').execute()
        trainers_list = trainers_response.data if trainers_response.data else []

        # Get members with filters
        if filter_trainer_id:
            # Get regular members assigned to this trainer
            response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('trainer_id', filter_trainer_id).order('created_at', desc=True).execute()
            regular_members = response.data if response.data else []

            # Also get OT members assigned to this trainer via ot_assignments
            ot_assignments_response = supabase.table('ot_assignments').select(
                'member_id, status, session_number'
            ).eq('trainer_id', filter_trainer_id).in_('status', ['assigned', 'scheduled', 'completed']).execute()

            ot_member_ids = []
            ot_session_counts = {}
            ot_first_session_numbers = {}
            if ot_assignments_response.data:
                for ot in ot_assignments_response.data:
                    mid = ot['member_id']
                    if mid not in ot_member_ids:
                        ot_member_ids.append(mid)
                        ot_first_session_numbers[mid] = ot['session_number']
                    ot_session_counts[mid] = ot_session_counts.get(mid, 0) + 1

            ot_members = []
            if ot_member_ids:
                ot_response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').in_('id', ot_member_ids).execute()
                if ot_response.data:
                    trainer_name_resp = supabase.table('users').select('name').eq('id', filter_trainer_id).execute()
                    trainer_display_name = trainer_name_resp.data[0]['name'] if trainer_name_resp.data else ''
                    for m in ot_response.data:
                        m['ot_session_number'] = ot_first_session_numbers.get(m['id'], 1)
                        m['sessions'] = ot_session_counts.get(m['id'], 1)
                        m['trainer'] = {'name': trainer_display_name}
                    ot_members = ot_response.data

            response = type('obj', (object,), {'data': regular_members + ot_members})()

            trainer_response = supabase.table('users').select('name').eq('id', filter_trainer_id).execute()
            if trainer_response.data:
                filter_trainer_name = trainer_response.data[0]['name']
        elif filter_branch_id:
            # Get all trainers in selected branch
            branch_trainers = supabase.table('users').select('id').eq('branch_id', filter_branch_id).eq('role', 'trainer').execute()
            branch_trainer_ids = [t['id'] for t in branch_trainers.data] if branch_trainers.data else []
            if branch_trainer_ids:
                response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').in_('trainer_id', branch_trainer_ids).order('created_at', desc=True).execute()
            else:
                response = type('obj', (object,), {'data': []})()
        else:
            # No filter - show empty until selection
            response = type('obj', (object,), {'data': []})()

    elif user['role'] == 'branch_admin':
        # Get trainers in this branch for filter
        trainers_response = supabase.table('users').select('id, name').eq('branch_id', user['branch_id']).eq('role', 'trainer').order('name').execute()
        trainers_list = trainers_response.data if trainers_response.data else []
        trainer_ids = [t['id'] for t in trainers_list]

        if filter_trainer_id and filter_trainer_id in trainer_ids:
            # Get regular members assigned to this trainer
            response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('trainer_id', filter_trainer_id).order('created_at', desc=True).execute()
            regular_members = response.data if response.data else []

            # Also get OT members assigned to this trainer via ot_assignments
            ot_assignments_response = supabase.table('ot_assignments').select(
                'member_id, status, session_number'
            ).eq('trainer_id', filter_trainer_id).in_('status', ['assigned', 'scheduled', 'completed']).execute()

            ot_member_ids = []
            ot_session_counts = {}
            ot_first_session_numbers = {}
            if ot_assignments_response.data:
                for ot in ot_assignments_response.data:
                    mid = ot['member_id']
                    if mid not in ot_member_ids:
                        ot_member_ids.append(mid)
                        ot_first_session_numbers[mid] = ot['session_number']
                    ot_session_counts[mid] = ot_session_counts.get(mid, 0) + 1

            ot_members = []
            if ot_member_ids:
                ot_response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').in_('id', ot_member_ids).execute()
                if ot_response.data:
                    trainer_name_resp = supabase.table('users').select('name').eq('id', filter_trainer_id).execute()
                    trainer_display_name = trainer_name_resp.data[0]['name'] if trainer_name_resp.data else ''
                    for m in ot_response.data:
                        m['ot_session_number'] = ot_first_session_numbers.get(m['id'], 1)
                        m['sessions'] = ot_session_counts.get(m['id'], 1)
                        m['trainer'] = {'name': trainer_display_name}
                    ot_members = ot_response.data

            response = type('obj', (object,), {'data': regular_members + ot_members})()

            trainer_response = supabase.table('users').select('name').eq('id', filter_trainer_id).execute()
            if trainer_response.data:
                filter_trainer_name = trainer_response.data[0]['name']
        else:
            # No trainer selected - show empty until selection
            response = type('obj', (object,), {'data': []})()

    else:  # trainer
        # Get regular members assigned to trainer
        response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('trainer_id', user['id']).neq('member_type', 'OT회원').order('created_at', desc=True).execute()
        regular_members = response.data if response.data else []

        # Get OT members assigned to this trainer via ot_assignments
        # Include 'completed' status so trainers can still see their completed OT sessions
        ot_assignments_response = supabase.table('ot_assignments').select(
            'member_id, status, session_number'
        ).eq('trainer_id', user['id']).in_('status', ['assigned', 'scheduled', 'completed']).execute()

        ot_member_ids = []
        ot_session_counts = {}  # {member_id: count of allocated sessions to this trainer}
        ot_first_session_numbers = {}  # {member_id: first session_number for display}
        if ot_assignments_response.data:
            for ot in ot_assignments_response.data:
                mid = ot['member_id']
                if mid not in ot_member_ids:
                    ot_member_ids.append(mid)
                    ot_first_session_numbers[mid] = ot['session_number']
                # Count total sessions allocated to this trainer
                ot_session_counts[mid] = ot_session_counts.get(mid, 0) + 1

        ot_members = []
        if ot_member_ids:
            ot_response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').in_('id', ot_member_ids).execute()
            if ot_response.data:
                for m in ot_response.data:
                    m['ot_session_number'] = ot_first_session_numbers.get(m['id'], 1)
                    # Override sessions with count allocated to this trainer (not total OT sessions)
                    m['sessions'] = ot_session_counts.get(m['id'], 1)
                    # Set trainer name for display (assigned trainer, not original)
                    m['trainer'] = {'name': session['user']['name']}
                ot_members = ot_response.data

        # Combine regular and OT members
        response = type('obj', (object,), {'data': regular_members + ot_members})()

    members_list = response.data if response.data else []

    # For admins, sort to show regular members first, OT members at bottom
    if user['role'] in ['main_admin', 'branch_admin']:
        regular_members = [m for m in members_list if m.get('member_type') != 'OT회원']
        ot_members = [m for m in members_list if m.get('member_type') == 'OT회원']
        members_list = regular_members + ot_members

    # Get all member IDs
    member_ids = [m['id'] for m in members_list]

    # For trainers, track which members are OT members (need to filter their schedules by trainer_id)
    ot_member_ids_set = set()
    if user['role'] == 'trainer':
        ot_member_ids_set = set([m['id'] for m in members_list if m.get('member_type') == 'OT회원'])

    # Fetch all schedules for these members in the selected month
    if member_ids:
        schedules_response = supabase.table('schedules').select('*').in_('member_id', member_ids).gte('schedule_date', month_start.isoformat()).lte('schedule_date', month_end.isoformat()).execute()
        schedules = schedules_response.data if schedules_response.data else []

        # Also get total completed sessions for each member (all time)
        # Include trainer_id so we can filter for trainers viewing OT members
        all_schedules_response = supabase.table('schedules').select('member_id, trainer_id, status').in_('member_id', member_ids).eq('status', '수업 완료').execute()
        all_completed = all_schedules_response.data if all_schedules_response.data else []
    else:
        schedules = []
        all_completed = []

    # Count completed sessions per member
    # For trainers viewing OT members, only count their own completed sessions
    completed_counts = {}
    for s in all_completed:
        mid = s['member_id']
        # For trainers viewing OT members, only count their own completed sessions
        if user['role'] == 'trainer' and mid in ot_member_ids_set:
            if s.get('trainer_id') != user['id']:
                continue
        completed_counts[mid] = completed_counts.get(mid, 0) + 1

    # Organize schedules by member and date (list of schedules per date)
    # For trainers viewing OT members, only include their own schedules
    schedule_map = {}  # {member_id: {date: [schedules]}}
    for s in schedules:
        mid = s['member_id']
        # For trainers viewing OT members, only include their own schedules
        if user['role'] == 'trainer' and mid in ot_member_ids_set:
            if s.get('trainer_id') != user['id']:
                continue
        date = s['schedule_date']
        if mid not in schedule_map:
            schedule_map[mid] = {}
        if date not in schedule_map[mid]:
            schedule_map[mid][date] = []
        schedule_map[mid][date].append(s)

    # Add calculated fields to each member
    for member in members_list:
        member['contract_amount'] = member['sessions'] * member['unit_price']
        member['completed_sessions'] = completed_counts.get(member['id'], 0)
        member['remaining_sessions'] = member['sessions'] - member['completed_sessions']
        member['schedule_map'] = schedule_map.get(member['id'], {})

    # Add display_name for duplicate name detection
    add_display_names_to_members(members_list)

    return render_template('members.html',
                         user=user,
                         members=members_list,
                         month_days=month_days,
                         selected_month=month_start.strftime('%Y-%m'),
                         selected_year=month_start.year,
                         selected_month_num=month_start.month,
                         branches=branches_list,
                         trainers=trainers_list,
                         filter_branch_id=filter_branch_id,
                         filter_trainer_id=filter_trainer_id,
                         filter_trainer_name=filter_trainer_name)


@app.route('/members/add', methods=['GET', 'POST'])
@login_required
def add_member():
    user = session['user']
    trainers = []

    # Only admins can select trainer
    if user['role'] in ['main_admin', 'branch_admin']:
        if user['role'] == 'main_admin':
            response = supabase.table('users').select('id, name, branch_id').eq('role', 'trainer').execute()
        else:
            response = supabase.table('users').select('id, name').eq('role', 'trainer').eq('branch_id', user['branch_id']).execute()
        trainers = response.data if response.data else []

    if request.method == 'POST':
        # Get form data
        member_name = request.form.get('member_name')
        phone = request.form.get('phone')
        payment_method = request.form.get('payment_method')
        sessions = request.form.get('sessions')
        unit_price = request.form.get('unit_price')
        channel = request.form.get('channel')
        signature = request.form.get('signature')
        member_type = request.form.get('member_type', '일반회원')

        # Optional fields
        age = request.form.get('age')
        gender = request.form.get('gender')
        occupation = request.form.get('occupation')
        special_notes = request.form.get('special_notes')

        # Determine trainer_id
        if user['role'] == 'trainer':
            trainer_id = user['id']
        else:
            trainer_id = request.form.get('trainer_id')

        # Validate required fields (payment fields optional for OT members)
        if member_type == 'OT회원':
            # OT members: only need basic info (no trainer required - goes to pool)
            if not all([member_name, phone, channel]):
                flash('모든 필수 항목을 입력해주세요.', 'error')
                return render_template('add_member.html', user=user, trainers=trainers)
            # OT members need 횟수 (sessions)
            sessions = sessions or '1'
            unit_price = '0'
            payment_method = '무료'
        else:
            # Regular members: all payment fields required
            if not all([member_name, phone, payment_method, sessions, unit_price, channel, trainer_id]):
                flash('모든 필수 항목을 입력해주세요.', 'error')
                return render_template('add_member.html', user=user, trainers=trainers)

        # Insert member into database
        try:
            member_data = {
                'member_name': member_name,
                'phone': phone,
                'payment_method': payment_method,
                'sessions': int(sessions),
                'unit_price': int(unit_price),
                'channel': channel,
                'signature': signature,
                'created_by': user['id'],
                'member_type': member_type
            }

            # For OT members: no trainer assignment, goes to branch admin pool
            if member_type == 'OT회원':
                member_data['ot_status'] = 'unassigned'
                member_data['ot_remaining_sessions'] = int(sessions)  # Track unassigned sessions
                # Get branch_id from current user for filtering
                if user['role'] == 'trainer':
                    member_data['branch_id'] = user.get('branch_id')
                elif user['role'] == 'branch_admin':
                    member_data['branch_id'] = user.get('branch_id')
                # trainer_id stays NULL for OT members
            else:
                member_data['trainer_id'] = trainer_id

            # Add optional fields if provided
            if age:
                member_data['age'] = int(age)
            if gender:
                member_data['gender'] = gender
            if occupation:
                member_data['occupation'] = occupation
            if special_notes:
                member_data['special_notes'] = special_notes

            # Handle InBody photos
            inbody_photos_json = request.form.get('inbody_photos')
            if inbody_photos_json:
                try:
                    import json
                    inbody_photos = json.loads(inbody_photos_json)
                    if inbody_photos:
                        member_data['inbody_photos'] = inbody_photos
                except:
                    pass

            supabase.table('members').insert(member_data).execute()
            if member_type == 'OT회원':
                flash('OT 회원이 등록되었습니다. 지점장이 트레이너에게 배정합니다.', 'success')
                return redirect(url_for('ot_members'))
            else:
                flash('회원이 성공적으로 등록되었습니다.', 'success')
                return redirect(url_for('members'))
        except Exception as e:
            flash(f'회원 등록 중 오류가 발생했습니다: {str(e)}', 'error')

    return render_template('add_member.html', user=user, trainers=trainers)


@app.route('/members/<member_id>')
@login_required
def view_member(member_id):
    user = session['user']

    response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('id', member_id).execute()

    if not response.data:
        flash('회원을 찾을 수 없습니다.', 'error')
        return redirect(url_for('members'))

    member = response.data[0]

    # Check access permissions
    if user['role'] == 'trainer' and member['trainer_id'] != user['id']:
        flash('접근 권한이 없습니다.', 'error')
        return redirect(url_for('members'))

    if user['role'] == 'branch_admin':
        trainer = supabase.table('users').select('branch_id').eq('id', member['trainer_id']).execute()
        if trainer.data and trainer.data[0]['branch_id'] != user['branch_id']:
            flash('접근 권한이 없습니다.', 'error')
            return redirect(url_for('members'))

    return render_template('view_member.html', user=user, member=member)


# API endpoint to get member details for modal
@app.route('/api/member/<member_id>')
@login_required
def api_get_member(member_id):
    user = session['user']

    response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('id', member_id).execute()

    if not response.data:
        return jsonify({'success': False, 'error': '회원을 찾을 수 없습니다.'})

    member = response.data[0]

    # Check access permissions
    if user['role'] == 'trainer':
        # Check if trainer owns this member directly OR has an OT assignment
        is_direct_member = member['trainer_id'] == user['id']
        is_ot_assigned = False
        if not is_direct_member and member.get('member_type') == 'OT회원':
            ot_check = supabase.table('ot_assignments').select('id').eq(
                'member_id', member_id
            ).eq('trainer_id', user['id']).in_('status', ['assigned', 'scheduled', 'completed']).execute()
            is_ot_assigned = bool(ot_check.data)
        if not is_direct_member and not is_ot_assigned:
            return jsonify({'success': False, 'error': '접근 권한이 없습니다.'})

    if user['role'] == 'branch_admin':
        # For OT members, check if they belong to this branch
        if member.get('member_type') == 'OT회원':
            if member.get('branch_id') != user['branch_id']:
                return jsonify({'success': False, 'error': '접근 권한이 없습니다.'})
        elif member['trainer_id']:
            trainer = supabase.table('users').select('branch_id').eq('id', member['trainer_id']).execute()
            if trainer.data and trainer.data[0]['branch_id'] != user['branch_id']:
                return jsonify({'success': False, 'error': '접근 권한이 없습니다.'})

    # Get OT session number if OT member
    ot_session_number = None
    if member.get('member_type') == 'OT회원':
        ot_session_number = get_ot_session_number(member_id)

    # Calculate completed sessions from schedules
    completed_resp = supabase.table('schedules').select('id', count='exact').eq('member_id', member_id).eq('status', '수업 완료').execute()
    completed_sessions = completed_resp.count if completed_resp.count else 0

    # Build response data
    member_data = {
        'id': member['id'],
        'member_name': member['member_name'],
        'phone': member['phone'],
        'payment_method': member['payment_method'],
        'sessions': member['sessions'],
        'unit_price': member['unit_price'],
        'channel': member['channel'],
        'signature': member.get('signature'),
        'created_at': member.get('created_at'),
        'refund_status': member.get('refund_status'),
        'refund_amount': member.get('refund_amount'),
        'transfer_status': member.get('transfer_status'),
        'completed_sessions': completed_sessions,
        'trainer_name': member['trainer']['name'] if member.get('trainer') else None,
        # New fields
        'member_type': member.get('member_type', '일반회원'),
        'age': member.get('age'),
        'gender': member.get('gender'),
        'occupation': member.get('occupation'),
        'special_notes': member.get('special_notes'),
        'ot_status': member.get('ot_status'),
        'ot_deadline': member.get('ot_deadline'),
        'ot_extended': member.get('ot_extended'),
        'ot_session_number': ot_session_number,
        'inbody_photos': member.get('inbody_photos', []),
    }

    return jsonify({'success': True, 'member': member_data})


@app.route('/api/member/<member_id>/inbody', methods=['POST'])
@login_required
def api_add_inbody_photo(member_id):
    """Add an InBody photo to a member."""
    user = session['user']
    data = request.get_json()
    photo_data = data.get('photo')

    if not photo_data:
        return jsonify({'success': False, 'error': '사진 데이터가 없습니다.'})

    try:
        # Get current member data
        response = supabase.table('members').select('inbody_photos, trainer_id').eq('id', member_id).execute()
        if not response.data:
            return jsonify({'success': False, 'error': '회원을 찾을 수 없습니다.'})

        member = response.data[0]

        # Check permissions (trainer can only update their own members)
        if user['role'] == 'trainer' and member['trainer_id'] != user['id']:
            return jsonify({'success': False, 'error': '접근 권한이 없습니다.'})

        # Get existing photos or empty list
        photos = member.get('inbody_photos') or []
        photos.append(photo_data)

        # Update member with new photos
        supabase.table('members').update({'inbody_photos': photos}).eq('id', member_id).execute()

        return jsonify({'success': True, 'photos': photos})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/member/<member_id>/inbody/<int:photo_index>', methods=['DELETE'])
@login_required
def api_delete_inbody_photo(member_id, photo_index):
    """Delete an InBody photo from a member."""
    user = session['user']

    try:
        # Get current member data
        response = supabase.table('members').select('inbody_photos, trainer_id').eq('id', member_id).execute()
        if not response.data:
            return jsonify({'success': False, 'error': '회원을 찾을 수 없습니다.'})

        member = response.data[0]

        # Check permissions
        if user['role'] == 'trainer' and member['trainer_id'] != user['id']:
            return jsonify({'success': False, 'error': '접근 권한이 없습니다.'})

        # Get existing photos
        photos = member.get('inbody_photos') or []

        if photo_index < 0 or photo_index >= len(photos):
            return jsonify({'success': False, 'error': '사진을 찾을 수 없습니다.'})

        # Remove photo at index
        photos.pop(photo_index)

        # Update member with remaining photos
        supabase.table('members').update({'inbody_photos': photos}).eq('id', member_id).execute()

        return jsonify({'success': True, 'photos': photos})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/schedule/<schedule_id>/notes', methods=['POST'])
@login_required
def api_update_session_notes(schedule_id):
    """Update session notes for a schedule entry."""
    user = session['user']
    data = request.get_json()
    session_notes = data.get('session_notes', '')

    # Get the schedule entry
    response = supabase.table('schedules').select('*, member:members(trainer_id)').eq('id', schedule_id).execute()

    if not response.data:
        return jsonify({'success': False, 'error': '세션을 찾을 수 없습니다.'})

    schedule = response.data[0]

    # Check access permissions - trainer can only edit their own members' sessions
    if user['role'] == 'trainer':
        if schedule['member'] and schedule['member']['trainer_id'] != user['id']:
            return jsonify({'success': False, 'error': '접근 권한이 없습니다.'})

    # Update session notes
    try:
        supabase.table('schedules').update({
            'session_notes': session_notes
        }).eq('id', schedule_id).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# Admin routes for managing trainers (트레이너 관리)
@app.route('/trainers')
@role_required('main_admin', 'branch_admin')
def trainers():
    user = session['user']
    selected_branch_id = request.args.get('branch_id')

    # Get branches for filter dropdown (main_admin only)
    branches = []
    if user['role'] == 'main_admin':
        branches_response = supabase.table('branches').select('*').execute()
        branches = branches_response.data if branches_response.data else []

    # Build query based on role and filter
    if user['role'] == 'main_admin':
        query = supabase.table('users').select('*, branch:branches(name)').eq('role', 'trainer')
        if selected_branch_id:
            query = query.eq('branch_id', selected_branch_id)
        response = query.execute()
    else:
        response = supabase.table('users').select('*, branch:branches(name)').eq('branch_id', user['branch_id']).eq('role', 'trainer').execute()

    trainers_list = response.data if response.data else []

    return render_template('trainers.html', user=user, trainers=trainers_list, branches=branches, selected_branch_id=selected_branch_id)


@app.route('/trainers/add', methods=['GET', 'POST'])
@role_required('main_admin', 'branch_admin')
def add_trainer():
    user = session['user']

    # Get branches for selection (only for main_admin)
    if user['role'] == 'main_admin':
        branches_response = supabase.table('branches').select('*').execute()
        branches = branches_response.data if branches_response.data else []
    else:
        branches = []

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        name = request.form.get('name')

        if user['role'] == 'main_admin':
            branch_id = request.form.get('branch_id')
        else:
            branch_id = user['branch_id']

        if not all([email, password, name, branch_id]):
            flash('모든 필수 항목을 입력해주세요.', 'error')
            return render_template('add_trainer.html', user=user, branches=branches)

        try:
            trainer_data = {
                'email': email,
                'password_hash': password,  # In production, hash this!
                'name': name,
                'role': 'trainer',  # Always trainer
                'branch_id': branch_id
            }

            supabase.table('users').insert(trainer_data).execute()
            flash('트레이너가 성공적으로 등록되었습니다.', 'success')
            return redirect(url_for('trainers'))
        except Exception as e:
            flash(f'트레이너 등록 중 오류가 발생했습니다: {str(e)}', 'error')

    return render_template('add_trainer.html', user=user, branches=branches)


# Branch management routes (지점 관리) - Main Admin only
@app.route('/branches')
@role_required('main_admin')
def branches():
    user = session['user']

    response = supabase.table('branches').select('*').order('name').execute()
    branches_list = response.data if response.data else []

    # Get counts for each branch
    for branch in branches_list:
        trainers_count = supabase.table('users').select('id', count='exact').eq('branch_id', branch['id']).eq('role', 'trainer').execute()
        admins_count = supabase.table('users').select('id', count='exact').eq('branch_id', branch['id']).eq('role', 'branch_admin').execute()
        branch['trainer_count'] = len(trainers_count.data) if trainers_count.data else 0
        branch['admin_count'] = len(admins_count.data) if admins_count.data else 0

    return render_template('branches.html', user=user, branches=branches_list)


@app.route('/branches/add', methods=['GET', 'POST'])
@role_required('main_admin')
def add_branch():
    user = session['user']

    if request.method == 'POST':
        name = request.form.get('name')

        if not name:
            flash('지점명을 입력해주세요.', 'error')
            return render_template('add_branch.html', user=user)

        try:
            supabase.table('branches').insert({'name': name}).execute()
            flash('지점이 성공적으로 등록되었습니다.', 'success')
            return redirect(url_for('branches'))
        except Exception as e:
            flash(f'지점 등록 중 오류가 발생했습니다: {str(e)}', 'error')

    return render_template('add_branch.html', user=user)


# Branch Admin (지점장) management routes - Main Admin only
@app.route('/branch-admins')
@role_required('main_admin')
def branch_admins():
    user = session['user']
    selected_branch_id = request.args.get('branch_id')

    # Get branches for filter dropdown
    branches_response = supabase.table('branches').select('*').execute()
    branches = branches_response.data if branches_response.data else []

    # Build query with optional filter
    query = supabase.table('users').select('*, branch:branches(name)').eq('role', 'branch_admin')
    if selected_branch_id:
        query = query.eq('branch_id', selected_branch_id)
    response = query.execute()

    admins_list = response.data if response.data else []

    return render_template('branch_admins.html', user=user, admins=admins_list, branches=branches, selected_branch_id=selected_branch_id)


@app.route('/branch-admins/add', methods=['GET', 'POST'])
@role_required('main_admin')
def add_branch_admin():
    user = session['user']

    branches_response = supabase.table('branches').select('*').execute()
    branches = branches_response.data if branches_response.data else []

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        name = request.form.get('name')
        branch_id = request.form.get('branch_id')

        if not all([email, password, name, branch_id]):
            flash('모든 필수 항목을 입력해주세요.', 'error')
            return render_template('add_branch_admin.html', user=user, branches=branches)

        try:
            admin_data = {
                'email': email,
                'password_hash': password,  # In production, hash this!
                'name': name,
                'role': 'branch_admin',
                'branch_id': branch_id
            }

            supabase.table('users').insert(admin_data).execute()
            flash('지점장이 성공적으로 등록되었습니다.', 'success')
            return redirect(url_for('branch_admins'))
        except Exception as e:
            flash(f'지점장 등록 중 오류가 발생했습니다: {str(e)}', 'error')

    return render_template('add_branch_admin.html', user=user, branches=branches)


# Helper function to auto-cancel past uncompleted sessions
def auto_cancel_past_sessions():
    """Mark past sessions as cancelled if they weren't completed"""
    today = datetime.now(KST).date()
    try:
        # Find all planned sessions from past dates
        supabase.table('schedules').update({
            'status': '수업 취소'
        }).eq('status', '수업 계획').lt('schedule_date', today.isoformat()).execute()
    except Exception as e:
        print(f"Auto-cancel error: {e}")


# Schedule routes
@app.route('/schedule')
@login_required
def schedule():
    user = session['user']

    # Auto-cancel past uncompleted sessions
    auto_cancel_past_sessions()

    # Get date from query param or use today
    date_str = request.args.get('date')
    if date_str:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = datetime.now(KST).date()

    # Calculate week range (Monday to Sunday)
    week_start = selected_date - timedelta(days=selected_date.weekday())
    week_end = week_start + timedelta(days=6)

    # Get trainers list for admin filter
    trainers_list = []
    branches_list = []
    selected_trainer_id = request.args.get('trainer_id')
    filter_branch_id = request.args.get('branch_id')

    if user['role'] == 'main_admin':
        # Get all branches for filter
        branches_response = supabase.table('branches').select('*').order('name').execute()
        branches_list = branches_response.data if branches_response.data else []

        # Filter trainers by branch if selected
        if filter_branch_id:
            trainers_response = supabase.table('users').select('id, name').eq('role', 'trainer').eq('branch_id', filter_branch_id).execute()
        else:
            trainers_response = supabase.table('users').select('id, name').eq('role', 'trainer').execute()
        trainers_list = trainers_response.data if trainers_response.data else []
    elif user['role'] == 'branch_admin':
        trainers_response = supabase.table('users').select('id, name').eq('role', 'trainer').eq('branch_id', user['branch_id']).execute()
        trainers_list = trainers_response.data if trainers_response.data else []

    # Get schedules based on role
    if user['role'] == 'trainer':
        query_trainer_id = user['id']
    elif selected_trainer_id:
        query_trainer_id = selected_trainer_id
    else:
        query_trainer_id = None

    # Build query
    query = supabase.table('schedules').select(
        '*, member:members!schedules_member_id_fkey(member_name, phone, trainer_id), trainer:users!schedules_trainer_id_fkey(name)'
    ).gte('schedule_date', week_start.isoformat()).lte('schedule_date', week_end.isoformat())

    if query_trainer_id:
        query = query.eq('trainer_id', query_trainer_id)
    elif user['role'] == 'branch_admin':
        # Get all trainers in branch
        branch_trainers = supabase.table('users').select('id').eq('branch_id', user['branch_id']).execute()
        trainer_ids = [t['id'] for t in branch_trainers.data] if branch_trainers.data else []
        if trainer_ids:
            query = query.in_('trainer_id', trainer_ids)

    response = query.order('schedule_date').order('start_time').execute()
    schedules = response.data if response.data else []

    # Collect all members from schedules for duplicate name detection
    schedule_members = []
    for s in schedules:
        if s.get('member'):
            schedule_members.append({
                'member_name': s['member'].get('member_name'),
                'phone': s['member'].get('phone', ''),
                'trainer_id': s.get('trainer_id')
            })

    # Add display_name to each schedule's member
    for s in schedules:
        if s.get('member'):
            member_info = {
                'member_name': s['member'].get('member_name'),
                'phone': s['member'].get('phone', ''),
                'trainer_id': s.get('trainer_id')
            }
            s['member']['display_name'] = get_display_name(member_info, schedule_members)

    # Organize schedules by date and time
    schedule_grid = {}
    time_slots = ['06:00', '07:00', '08:00', '09:00', '10:00', '11:00', '12:00',
                  '13:00', '14:00', '15:00', '16:00', '17:00', '18:00', '19:00',
                  '20:00', '21:00', '22:00']

    # Initialize grid
    for i in range(7):
        day = week_start + timedelta(days=i)
        schedule_grid[day.isoformat()] = {slot: None for slot in time_slots}

    # Fill in schedules
    for s in schedules:
        date_key = s['schedule_date']
        time_key = s['start_time'][:5]  # Get HH:MM
        if date_key in schedule_grid and time_key in schedule_grid[date_key]:
            schedule_grid[date_key][time_key] = s

    # Generate week days for template
    week_days = []
    day_names = ['월', '화', '수', '목', '금', '토', '일']
    for i in range(7):
        day = week_start + timedelta(days=i)
        week_days.append({
            'date': day.isoformat(),
            'day_name': day_names[i],
            'day_num': day.day,
            'is_today': day == datetime.now(KST).date()
        })

    # Get members for quick-add feature (only for selected trainer)
    # Filter: not refunded, not transferred, and has remaining sessions
    members_list = []
    if user['role'] == 'trainer':
        members_response = supabase.table('members').select(
            'id, member_name, phone, sessions, trainer_id, created_at, refund_status, transfer_status'
        ).eq('trainer_id', user['id']).order('created_at').execute()
        raw_members = members_response.data if members_response.data else []
    elif selected_trainer_id:
        # For admins, only load members of the selected trainer
        members_response = supabase.table('members').select(
            'id, member_name, phone, sessions, trainer_id, created_at, refund_status, transfer_status'
        ).eq('trainer_id', selected_trainer_id).order('created_at').execute()
        raw_members = members_response.data if members_response.data else []
    else:
        raw_members = []

    # Filter out refunded, transferred, OT members, and those with no remaining sessions
    for member in raw_members:
        # Skip OT members (they use separate OT scheduling flow)
        if member.get('member_type') == 'OT회원':
            continue
        # Skip refunded members
        if member.get('refund_status') == 'refunded':
            continue
        # Skip transferred members
        if member.get('transfer_status') == 'transferred':
            continue
        # Check remaining sessions
        completed_resp = supabase.table('schedules').select('id', count='exact').eq('member_id', member['id']).eq('status', '수업 완료').execute()
        completed_sessions = completed_resp.count if completed_resp.count else 0
        remaining = member['sessions'] - completed_sessions
        if remaining > 0:
            member['remaining_sessions'] = remaining
            members_list.append(member)

    # Deduplicate members with same name+phone (show only once per person)
    members_list = deduplicate_members_for_dropdown(members_list)

    # Add display_name for duplicate name detection
    add_display_names_to_members(members_list)

    # Get OT assignments for trainer (show in schedule page)
    ot_assignments_list = []
    near_deadline_ots = []  # OTs that need extension popup (1-2 days remaining, not yet extended)
    if user['role'] == 'trainer':
        ot_response = supabase.table('ot_assignments').select(
            '*, member:members!ot_assignments_member_id_fkey(id, member_name, phone, sessions)'
        ).eq('trainer_id', user['id']).eq('status', 'assigned').order('deadline').execute()
        if ot_response.data:
            for ot in ot_response.data:
                if ot.get('deadline'):
                    deadline = parse_datetime(ot['deadline'])
                    ot['days_remaining'] = (deadline.date() - datetime.now(KST).date()).days
                    # Check if near deadline (0-2 days) and not extended
                    if 0 <= ot['days_remaining'] <= 2 and not ot.get('extended'):
                        near_deadline_ots.append(ot)
                else:
                    ot['days_remaining'] = None
            ot_assignments_list = ot_response.data

    return render_template('schedule.html',
                         user=user,
                         schedule_grid=schedule_grid,
                         week_days=week_days,
                         time_slots=time_slots,
                         selected_date=selected_date.isoformat(),
                         week_start=week_start.isoformat(),
                         trainers=trainers_list,
                         branches=branches_list,
                         selected_trainer_id=selected_trainer_id,
                         filter_branch_id=filter_branch_id,
                         members=members_list,
                         ot_assignments=ot_assignments_list,
                         near_deadline_ots=near_deadline_ots)


@app.route('/schedule/add', methods=['GET', 'POST'])
@login_required
def add_schedule():
    user = session['user']

    # Get members for this trainer
    if user['role'] == 'trainer':
        members_response = supabase.table('members').select('id, member_name, phone, trainer_id, created_at').eq('trainer_id', user['id']).order('created_at').execute()
    elif user['role'] in ['main_admin', 'branch_admin']:
        if user['role'] == 'main_admin':
            members_response = supabase.table('members').select('id, member_name, phone, trainer_id, created_at').order('created_at').execute()
        else:
            trainers = supabase.table('users').select('id').eq('branch_id', user['branch_id']).execute()
            trainer_ids = [t['id'] for t in trainers.data] if trainers.data else []
            members_response = supabase.table('members').select('id, member_name, phone, trainer_id, created_at').in_('trainer_id', trainer_ids).order('created_at').execute()

    members_list = members_response.data if members_response.data else []

    # Deduplicate members with same name+phone (show only once per person)
    members_list = deduplicate_members_for_dropdown(members_list)

    # Add display_name for duplicate name detection
    add_display_names_to_members(members_list)

    # Get trainers for admin
    trainers_list = []
    if user['role'] in ['main_admin', 'branch_admin']:
        if user['role'] == 'main_admin':
            trainers_response = supabase.table('users').select('id, name').eq('role', 'trainer').execute()
        else:
            trainers_response = supabase.table('users').select('id, name').eq('role', 'trainer').eq('branch_id', user['branch_id']).execute()
        trainers_list = trainers_response.data if trainers_response.data else []

    # Pre-fill date and time from query params
    prefill_date = request.args.get('date', datetime.now().date().isoformat())
    prefill_time = request.args.get('time', '09:00')

    if request.method == 'POST':
        member_id = request.form.get('member_id')
        schedule_date = request.form.get('schedule_date')
        start_time = request.form.get('start_time')
        end_time = request.form.get('end_time')
        notes = request.form.get('notes', '')

        # Determine trainer_id
        if user['role'] == 'trainer':
            trainer_id = user['id']
        else:
            trainer_id = request.form.get('trainer_id')

        if not all([member_id, schedule_date, start_time, end_time, trainer_id]):
            flash('모든 필수 항목을 입력해주세요.', 'error')
            return render_template('add_schedule.html', user=user, members=members_list,
                                 trainers=trainers_list, prefill_date=prefill_date, prefill_time=prefill_time)

        try:
            schedule_data = {
                'trainer_id': trainer_id,
                'member_id': member_id,
                'schedule_date': schedule_date,
                'start_time': start_time,
                'end_time': end_time,
                'notes': notes
            }

            supabase.table('schedules').insert(schedule_data).execute()
            flash('스케줄이 등록되었습니다.', 'success')
            return redirect(url_for('schedule', date=schedule_date))
        except Exception as e:
            if 'duplicate' in str(e).lower() or '23505' in str(e):
                flash('해당 시간에 이미 스케줄이 있습니다.', 'error')
            else:
                flash(f'스케줄 등록 중 오류가 발생했습니다: {str(e)}', 'error')

    return render_template('add_schedule.html', user=user, members=members_list,
                         trainers=trainers_list, prefill_date=prefill_date, prefill_time=prefill_time)


@app.route('/schedule/delete/<schedule_id>', methods=['POST'])
@login_required
def delete_schedule(schedule_id):
    user = session['user']

    # Check permission
    schedule_response = supabase.table('schedules').select('*').eq('id', schedule_id).execute()
    if not schedule_response.data:
        flash('스케줄을 찾을 수 없습니다.', 'error')
        return redirect(url_for('schedule'))

    schedule = schedule_response.data[0]

    # Check if it's a completed or cancelled schedule - only main_admin can modify
    if schedule.get('status') in ['수업 완료', '수업 취소']:
        if user['role'] != 'main_admin':
            flash('지난 수업에 대한 수정은 불가능합니다.', 'error')
            return redirect(url_for('schedule', date=schedule['schedule_date']))

    # Check if it's a past schedule date - only main_admin can modify
    today = datetime.now(KST).date()
    schedule_date = datetime.strptime(schedule['schedule_date'], '%Y-%m-%d').date()
    if schedule_date < today and user['role'] != 'main_admin':
        flash('지난 스케줄은 삭제할 수 없습니다.', 'error')
        return redirect(url_for('schedule', date=schedule['schedule_date']))

    # Only trainer who owns it or admins can delete
    if user['role'] == 'trainer' and schedule['trainer_id'] != user['id']:
        flash('삭제 권한이 없습니다.', 'error')
        return redirect(url_for('schedule'))

    try:
        supabase.table('schedules').delete().eq('id', schedule_id).execute()
        flash('스케줄이 삭제되었습니다.', 'success')
    except Exception as e:
        flash(f'스케줄 삭제 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('schedule', date=schedule['schedule_date']))


@app.route('/schedule/complete/<schedule_id>', methods=['GET', 'POST'])
@login_required
def complete_session(schedule_id):
    user = session['user']

    # Get schedule details
    schedule_response = supabase.table('schedules').select(
        '*, member:members!schedules_member_id_fkey(member_name)'
    ).eq('id', schedule_id).execute()

    if not schedule_response.data:
        flash('스케줄을 찾을 수 없습니다.', 'error')
        return redirect(url_for('schedule'))

    schedule_item = schedule_response.data[0]

    # Check permission - only trainer who owns it can complete
    if user['role'] == 'trainer' and schedule_item['trainer_id'] != user['id']:
        flash('완료 권한이 없습니다.', 'error')
        return redirect(url_for('schedule'))

    # Check if already completed or cancelled
    if schedule_item.get('status') != '수업 계획':
        flash('이미 처리된 수업입니다.', 'error')
        return redirect(url_for('schedule', date=schedule_item['schedule_date']))

    if request.method == 'POST':
        work_type = request.form.get('work_type')
        session_signature = request.form.get('session_signature')

        if not work_type:
            flash('근무 유형을 선택해주세요.', 'error')
            return render_template('complete_session.html', user=user, schedule=schedule_item)

        if not session_signature:
            flash('회원 서명을 받아주세요.', 'error')
            return render_template('complete_session.html', user=user, schedule=schedule_item)

        try:
            supabase.table('schedules').update({
                'status': '수업 완료',
                'work_type': work_type,
                'session_signature': session_signature,
                'completed_at': datetime.now(KST).isoformat()
            }).eq('id', schedule_id).execute()

            flash('수업이 완료 처리되었습니다.', 'success')
            return redirect(url_for('schedule', date=schedule_item['schedule_date']))
        except Exception as e:
            flash(f'수업 완료 처리 중 오류가 발생했습니다: {str(e)}', 'error')

    return render_template('complete_session.html', user=user, schedule=schedule_item)


@app.route('/schedule/cancel/<schedule_id>', methods=['POST'])
@login_required
def cancel_session(schedule_id):
    user = session['user']

    # Get schedule details
    schedule_response = supabase.table('schedules').select('*').eq('id', schedule_id).execute()

    if not schedule_response.data:
        flash('스케줄을 찾을 수 없습니다.', 'error')
        return redirect(url_for('schedule'))

    schedule_item = schedule_response.data[0]

    # Check permission
    if user['role'] == 'trainer' and schedule_item['trainer_id'] != user['id']:
        flash('취소 권한이 없습니다.', 'error')
        return redirect(url_for('schedule'))

    # Check if already completed or cancelled
    if schedule_item.get('status') != '수업 계획':
        flash('이미 처리된 수업입니다.', 'error')
        return redirect(url_for('schedule', date=schedule_item['schedule_date']))

    try:
        supabase.table('schedules').update({
            'status': '수업 취소'
        }).eq('id', schedule_id).execute()

        flash('수업이 취소되었습니다.', 'success')
    except Exception as e:
        flash(f'수업 취소 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('schedule', date=schedule_item['schedule_date']))


@app.route('/schedule/complete-ajax', methods=['POST'])
@login_required
def complete_session_ajax():
    """AJAX endpoint for completing a session from the popup modal"""
    user = session['user']
    data = request.get_json()

    schedule_id = data.get('schedule_id')
    work_type = data.get('work_type')
    session_signature = data.get('session_signature')
    session_notes = data.get('session_notes', '')

    if not schedule_id:
        return jsonify({'success': False, 'error': '스케줄 ID가 필요합니다.'}), 400

    if not work_type:
        return jsonify({'success': False, 'error': '근무 유형을 선택해주세요.'}), 400

    if not session_signature:
        return jsonify({'success': False, 'error': '회원 서명을 받아주세요.'}), 400

    # Get schedule details
    schedule_response = supabase.table('schedules').select('*').eq('id', schedule_id).execute()

    if not schedule_response.data:
        return jsonify({'success': False, 'error': '스케줄을 찾을 수 없습니다.'}), 404

    schedule_item = schedule_response.data[0]

    # Check permission - only trainer who owns it can complete
    if user['role'] == 'trainer' and schedule_item['trainer_id'] != user['id']:
        return jsonify({'success': False, 'error': '완료 권한이 없습니다.'}), 403

    # Check if already completed or cancelled
    if schedule_item.get('status') != '수업 계획':
        return jsonify({'success': False, 'error': '이미 처리된 수업입니다.'}), 400

    try:
        supabase.table('schedules').update({
            'status': '수업 완료',
            'work_type': work_type,
            'session_signature': session_signature,
            'session_notes': session_notes,
            'completed_at': datetime.now().isoformat()
        }).eq('id', schedule_id).execute()

        # If this is an OT schedule, update the assignment status to 'completed'
        ot_assignment_id = schedule_item.get('ot_assignment_id')
        if ot_assignment_id:
            try:
                supabase.table('ot_assignments').update({
                    'status': 'completed',
                    'completed_at': datetime.now().isoformat()
                }).eq('id', ot_assignment_id).execute()

                # Log the completion
                supabase.table('ot_assignment_history').insert({
                    'member_id': schedule_item.get('member_id'),
                    'trainer_id': schedule_item.get('trainer_id'),
                    'action': 'completed',
                    'action_by': user['id'],
                    'notes': f'수업 완료: {schedule_item.get("schedule_date")}'
                }).execute()
            except Exception as e:
                print(f"Error updating OT assignment to completed: {e}")

        # Check if OT member has completed all sessions
        member_id = schedule_item.get('member_id')
        if member_id:
            check_ot_session_completion(member_id)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'수업 완료 처리 중 오류가 발생했습니다: {str(e)}'}), 500


@app.route('/schedule/cancel-ajax', methods=['POST'])
@login_required
def cancel_session_ajax():
    """AJAX endpoint for cancelling a session from the popup modal"""
    user = session['user']
    data = request.get_json()

    schedule_id = data.get('schedule_id')

    if not schedule_id:
        return jsonify({'success': False, 'error': '스케줄 ID가 필요합니다.'}), 400

    # Get schedule details
    schedule_response = supabase.table('schedules').select('*').eq('id', schedule_id).execute()

    if not schedule_response.data:
        return jsonify({'success': False, 'error': '스케줄을 찾을 수 없습니다.'}), 404

    schedule_item = schedule_response.data[0]

    # Check permission
    if user['role'] == 'trainer' and schedule_item['trainer_id'] != user['id']:
        return jsonify({'success': False, 'error': '취소 권한이 없습니다.'}), 403

    # Check if already completed or cancelled
    if schedule_item.get('status') != '수업 계획':
        return jsonify({'success': False, 'error': '이미 처리된 수업입니다.'}), 400

    try:
        supabase.table('schedules').update({
            'status': '수업 취소'
        }).eq('id', schedule_id).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'수업 취소 중 오류가 발생했습니다: {str(e)}'}), 500


@app.route('/schedule/edit-status', methods=['POST'])
@role_required('main_admin')
def edit_schedule_status():
    """AJAX endpoint for main_admin to edit any schedule status"""
    data = request.get_json()

    schedule_id = data.get('schedule_id')
    new_status = data.get('status')
    work_type = data.get('work_type')

    if not schedule_id:
        return jsonify({'success': False, 'error': '스케줄 ID가 필요합니다.'}), 400

    if new_status not in ['수업 계획', '수업 완료', '수업 취소']:
        return jsonify({'success': False, 'error': '유효하지 않은 상태입니다.'}), 400

    # Get schedule details
    schedule_response = supabase.table('schedules').select('*').eq('id', schedule_id).execute()

    if not schedule_response.data:
        return jsonify({'success': False, 'error': '스케줄을 찾을 수 없습니다.'}), 404

    try:
        update_data = {'status': new_status}

        # If changing to 수업 완료, set work_type and completed_at
        if new_status == '수업 완료':
            update_data['work_type'] = work_type if work_type else '근무내'
            update_data['completed_at'] = datetime.now().isoformat()
        # If changing away from 수업 완료, clear work_type
        elif new_status == '수업 계획':
            update_data['work_type'] = None
            update_data['completed_at'] = None
            update_data['session_signature'] = None

        supabase.table('schedules').update(update_data).eq('id', schedule_id).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'상태 변경 중 오류가 발생했습니다: {str(e)}'}), 500


@app.route('/schedule/quick-add', methods=['POST'])
@login_required
def quick_add_schedule():
    """AJAX endpoint for quickly adding schedules by clicking on time slots"""
    user = session['user']

    data = request.get_json()
    member_id = data.get('member_id')
    schedule_date = data.get('date')
    start_time = data.get('time')
    ot_assignment_id = data.get('ot_assignment_id')  # For OT schedules

    if not all([member_id, schedule_date, start_time]):
        return jsonify({'success': False, 'error': '필수 정보가 누락되었습니다.'}), 400

    # Calculate end time (1 hour later)
    start_hour = int(start_time.split(':')[0])
    end_time = f"{start_hour + 1:02d}:00"

    # Get member to verify ownership and get trainer_id
    member_response = supabase.table('members').select('*').eq('id', member_id).execute()
    if not member_response.data:
        return jsonify({'success': False, 'error': '회원을 찾을 수 없습니다.'}), 404

    member = member_response.data[0]

    # Determine trainer_id
    if user['role'] == 'trainer':
        # Trainer can only add their own members (or OT members assigned to them)
        is_own_member = member['trainer_id'] == user['id']
        is_ot_assigned = False

        # Check if this is an OT member assigned to this trainer
        if not is_own_member and ot_assignment_id:
            ot_check = supabase.table('ot_assignments').select('id').eq(
                'id', ot_assignment_id
            ).eq('trainer_id', user['id']).eq('member_id', member_id).execute()
            is_ot_assigned = bool(ot_check.data)

        if not is_own_member and not is_ot_assigned:
            return jsonify({'success': False, 'error': '본인의 회원만 스케줄에 추가할 수 있습니다.'}), 403
        trainer_id = user['id']
    else:
        # Admin uses the member's assigned trainer (or the trainer from OT assignment)
        trainer_id = member['trainer_id']
        if not trainer_id and ot_assignment_id:
            ot_assignment = supabase.table('ot_assignments').select('trainer_id').eq('id', ot_assignment_id).execute()
            if ot_assignment.data:
                trainer_id = ot_assignment.data[0]['trainer_id']

    # For OT assignments, check the assignment status instead of regular session count
    target_member_id = member_id
    if ot_assignment_id:
        # Check if this OT assignment is still valid for scheduling
        ot_assignment = supabase.table('ot_assignments').select('id, status').eq('id', ot_assignment_id).execute()
        if not ot_assignment.data:
            return jsonify({'success': False, 'error': 'OT 배정을 찾을 수 없습니다.'}), 400

        ot_status = ot_assignment.data[0]['status']
        if ot_status not in ['assigned', 'scheduled']:
            return jsonify({
                'success': False,
                'error': f"이 OT 배정은 더 이상 스케줄을 추가할 수 없습니다. (상태: {ot_status})"
            }), 400

        # Check if this assignment already has a scheduled session
        existing_schedule = supabase.table('schedules').select('id').eq(
            'ot_assignment_id', ot_assignment_id
        ).in_('status', ['수업 계획', '수업 완료']).execute()
        if existing_schedule.data:
            return jsonify({
                'success': False,
                'error': '이 OT 배정에 대한 스케줄이 이미 존재합니다.'
            }), 400
    else:
        # Regular member - check remaining sessions for this person (same name + phone)
        session_info = get_remaining_sessions_for_person(
            member['member_name'],
            member['phone'],
            trainer_id
        )

        if session_info['total_remaining'] <= 0:
            return jsonify({
                'success': False,
                'error': f"'{member['member_name']}' 회원의 잔여 세션이 없습니다. 새로운 회원 등록을 먼저 진행해주세요."
            }), 400

        # Use the oldest entry with available sessions
        if session_info['available_entry']:
            target_member_id = session_info['available_entry']['id']

    try:
        schedule_data = {
            'trainer_id': trainer_id,
            'member_id': target_member_id,
            'schedule_date': schedule_date,
            'start_time': start_time,
            'end_time': end_time,
            'status': '수업 계획'
        }

        # Add ot_assignment_id if provided
        if ot_assignment_id:
            schedule_data['ot_assignment_id'] = ot_assignment_id

        result = supabase.table('schedules').insert(schedule_data).execute()

        if result.data:
            # If this is an OT schedule, update the assignment status to 'scheduled'
            if ot_assignment_id:
                try:
                    supabase.table('ot_assignments').update({
                        'status': 'scheduled'
                    }).eq('id', ot_assignment_id).execute()

                    # Log the action
                    supabase.table('ot_assignment_history').insert({
                        'member_id': member_id,
                        'trainer_id': trainer_id,
                        'action': 'scheduled',
                        'action_by': user['id'],
                        'notes': f'스케줄 등록: {schedule_date} {start_time}'
                    }).execute()
                except Exception as e:
                    print(f"Error updating OT assignment status: {e}")

            return jsonify({
                'success': True,
                'schedule_id': result.data[0]['id'],
                'member_name': member['member_name']
            })
        else:
            return jsonify({'success': False, 'error': '스케줄 추가에 실패했습니다.'}), 500

    except Exception as e:
        error_msg = str(e)
        if 'duplicate' in error_msg.lower() or '23505' in error_msg:
            return jsonify({'success': False, 'error': '해당 시간에 이미 스케줄이 있습니다.'}), 409
        return jsonify({'success': False, 'error': f'오류: {error_msg}'}), 500


@app.route('/schedule/quick-delete', methods=['POST'])
@login_required
def quick_delete_schedule():
    """AJAX endpoint for quickly deleting schedules"""
    user = session['user']

    data = request.get_json()
    schedule_id = data.get('schedule_id')

    if not schedule_id:
        return jsonify({'success': False, 'error': '스케줄 ID가 필요합니다.'}), 400

    # Get schedule to check permissions and date
    schedule_response = supabase.table('schedules').select('*').eq('id', schedule_id).execute()
    if not schedule_response.data:
        return jsonify({'success': False, 'error': '스케줄을 찾을 수 없습니다.'}), 404

    schedule_item = schedule_response.data[0]

    # Check if it's a completed or cancelled schedule - only main_admin can modify
    if schedule_item.get('status') in ['수업 완료', '수업 취소']:
        if user['role'] != 'main_admin':
            return jsonify({'success': False, 'error': '지난 수업에 대한 수정은 불가능합니다.'}), 403

    # Check if it's a past schedule date - only main_admin can modify
    today = datetime.now(KST).date()
    schedule_date = datetime.strptime(schedule_item['schedule_date'], '%Y-%m-%d').date()
    if schedule_date < today and user['role'] != 'main_admin':
        return jsonify({'success': False, 'error': '지난 스케줄은 삭제할 수 없습니다.'}), 403

    # Check permission
    if user['role'] == 'trainer' and schedule_item['trainer_id'] != user['id']:
        return jsonify({'success': False, 'error': '삭제 권한이 없습니다.'}), 403

    try:
        supabase.table('schedules').delete().eq('id', schedule_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'오류: {str(e)}'}), 500


# Salary / Incentive calculation
# Default tiers (used if no settings in database)
DEFAULT_INCENTIVE_TIERS = [
    (20000000, 5400000),
    (15000000, 4050000),
    (12000000, 3040000),
    (10000000, 2400000),
    (8500000, 1955000),
    (6500000, 1430000),
    (4500000, 1050000),
    (3000000, 480000),
]

DEFAULT_LESSON_FEE_TIERS = [
    (20000000, 35),
    (15000000, 35),
    (12000000, 35),
    (10000000, 34),
    (8500000, 33),
    (6500000, 32),
    (4500000, 31),
    (3000000, 30),
]


def get_salary_settings():
    """Load salary settings from database, or return defaults if not found"""
    try:
        response = supabase.table('salary_settings').select('*').execute()
        if response.data and len(response.data) > 0:
            settings = response.data[0]
            # Convert stored JSON arrays to tuples and sort by threshold descending
            # (calculation functions expect highest threshold first)
            incentive_tiers = [(t['threshold'], t['incentive']) for t in settings.get('incentive_tiers', [])]
            lesson_fee_tiers = [(t['threshold'], t['rate']) for t in settings.get('lesson_fee_tiers', [])]

            # Filter out the "under minimum" tier (tier 0 with incentive 0) and sort descending
            if incentive_tiers:
                incentive_tiers = [t for t in incentive_tiers if t[1] > 0 or t[0] > incentive_tiers[0][0]]
                incentive_tiers = sorted(incentive_tiers, key=lambda x: x[0], reverse=True)
            if lesson_fee_tiers:
                # For lesson fees, keep all tiers but sort descending
                lesson_fee_tiers = sorted(lesson_fee_tiers, key=lambda x: x[0], reverse=True)

            return {
                'incentive_tiers': incentive_tiers if incentive_tiers else DEFAULT_INCENTIVE_TIERS,
                'lesson_fee_tiers': lesson_fee_tiers if lesson_fee_tiers else DEFAULT_LESSON_FEE_TIERS,
                'master_threshold': settings.get('master_threshold', 9000000),
                'master_bonus': settings.get('master_bonus', 300000),
                'other_threshold': settings.get('other_threshold', 5000000),
                'other_rate': settings.get('other_rate', 40)
            }
    except Exception as e:
        print(f"Error loading salary settings: {e}")

    # Return defaults if no settings found or error
    return {
        'incentive_tiers': DEFAULT_INCENTIVE_TIERS,
        'lesson_fee_tiers': DEFAULT_LESSON_FEE_TIERS,
        'master_threshold': 9000000,
        'master_bonus': 300000,
        'other_threshold': 5000000,
        'other_rate': 40
    }


def calculate_incentive(sales_amount, settings=None):
    """Calculate 트레이너 인센티브 based on sales amount (매출 기준)
    - 450만원 미만: 0
    - 450만원 이상: 225,000원 고정
    - 650만원 이상: 520,000원 고정
    - 800만원 이상: 880,000원 고정
    - 1000만원 이상: 1,400,000원 고정
    - 1200만원 이상: 17% (매출의 %)
    - 1500만원 이상: 17% + 50만원 고정
    - 2000만원 이상: 17% + 100만원 고정
    """
    if sales_amount >= 20000000:  # 2000만원 이상
        return int(sales_amount * 0.17) + 1000000
    elif sales_amount >= 15000000:  # 1500만원 이상
        return int(sales_amount * 0.17) + 500000
    elif sales_amount >= 12000000:  # 1200만원 이상
        return int(sales_amount * 0.17)
    elif sales_amount >= 10000000:  # 1000만원 이상
        return 1400000
    elif sales_amount >= 8000000:  # 800만원 이상
        return 880000
    elif sales_amount >= 6500000:  # 650만원 이상
        return 520000
    elif sales_amount >= 4500000:  # 450만원 이상
        return 225000
    else:  # 450만원 미만
        return 0


def calculate_master_trainer_bonus(six_month_sales, settings=None):
    """Calculate Master Trainer 진급 bonus based on 6-month average sales
    Threshold is configurable (default: 9,000,000 average per month)
    """
    if settings is None:
        settings = get_salary_settings()
    threshold = settings.get('master_threshold', 9000000)
    bonus = settings.get('master_bonus', 300000)
    avg_sales = six_month_sales / 6
    if avg_sales >= threshold:
        return bonus
    return 0


def calculate_lesson_fee_rate(sales_amount, settings=None):
    """Calculate 수업료 (근무내) percentage based on sales tier
    - 300만원 미만: 10%
    - 300만원 이상: 30%
    - 450만원 이상: 31%
    - 650만원 이상: 32%
    - 800만원 이상: 33%
    - 1000만원 이상: 34%
    - 1200만원 이상: 35%
    """
    if sales_amount >= 12000000:  # 1200만원 이상
        return 35
    elif sales_amount >= 10000000:  # 1000만원 이상
        return 34
    elif sales_amount >= 8000000:  # 800만원 이상
        return 33
    elif sales_amount >= 6500000:  # 650만원 이상
        return 32
    elif sales_amount >= 4500000:  # 450만원 이상
        return 31
    elif sales_amount >= 3000000:  # 300만원 이상
        return 30
    else:  # 300만원 미만
        return 10


def calculate_lesson_fee_rate_other(sales_amount, settings=None):
    """Calculate 근무외 lesson fee percentage based on sales"""
    if settings is None:
        settings = get_salary_settings()
    other_threshold = settings.get('other_threshold', 5000000)
    other_rate = settings.get('other_rate', 40)
    if sales_amount > other_threshold:
        return other_rate
    return calculate_lesson_fee_rate(sales_amount, settings)


def calculate_class_incentive(class_count, sales_amount):
    """Calculate 수업당 인센 based on class count
    - 30개 이상: 400,000원
    - 50개 이상: 600,000원
    - 70개 이상: 800,000원
    - 100개 이상: 1,000,000원
    * 매출 300만원 이하 일시 수업 갯수 인센 적용 안됨
    """
    # Must have sales > 3,000,000 to receive class incentive
    if sales_amount <= 3000000:
        return 0

    if class_count >= 100:
        return 1000000
    elif class_count >= 70:
        return 800000
    elif class_count >= 50:
        return 600000
    elif class_count >= 30:
        return 400000
    return 0


def calculate_member_sales_contribution(member):
    """Calculate how much a member contributes to sales (considering WI 50% rule)"""
    contract_amount = member['sessions'] * member['unit_price']
    if member.get('channel') == 'WI':
        contract_amount = contract_amount * 0.5
    return contract_amount


def calculate_trainer_incentives_for_month(trainer_id, month_start, next_month, exclude_member_id=None, settings=None):
    """
    Calculate trainer's incentives (인센티브 + Master Trainer bonus) for a specific month.
    Optionally exclude a specific member from the calculation.
    Returns tuple: (total_incentives, sales_amount)
    """
    if settings is None:
        settings = get_salary_settings()

    # Get members created in the month
    members_response = supabase.table('members').select(
        'id, sessions, unit_price, channel, refund_status'
    ).eq('trainer_id', trainer_id).gte(
        'created_at', month_start.isoformat()
    ).lt('created_at', next_month.isoformat()).execute()

    members_list = members_response.data if members_response.data else []

    # Calculate sales, optionally excluding a member
    # Note: Refunded members are now included since their 'sessions' field
    # reflects only completed sessions (proportional refund logic)
    sales = 0
    for m in members_list:
        if exclude_member_id and m['id'] == exclude_member_id:
            continue
        sales += calculate_member_sales_contribution(m)

    # Calculate 6-month range for master trainer bonus
    six_month_start = month_start
    for _ in range(5):
        if six_month_start.month == 1:
            six_month_start = six_month_start.replace(year=six_month_start.year - 1, month=12)
        else:
            six_month_start = six_month_start.replace(month=six_month_start.month - 1)

    # Get 6-month members
    six_month_response = supabase.table('members').select(
        'id, sessions, unit_price, channel'
    ).eq('trainer_id', trainer_id).gte(
        'created_at', six_month_start.isoformat()
    ).lt('created_at', next_month.isoformat()).execute()

    six_month_members = six_month_response.data if six_month_response.data else []

    six_month_sales = 0
    for m in six_month_members:
        if exclude_member_id and m['id'] == exclude_member_id:
            continue
        six_month_sales += calculate_member_sales_contribution(m)

    # Calculate incentives using settings
    incentive = calculate_incentive(sales, settings)
    master_bonus = calculate_master_trainer_bonus(six_month_sales, settings)

    total_incentives = incentive + master_bonus
    return total_incentives, sales


def calculate_refund_deduction(member_id):
    """
    Calculate the refund deduction amount for a member.
    Returns tuple: (deduction_amount, original_month)
    """
    # Get member info
    member_response = supabase.table('members').select('*').eq('id', member_id).execute()
    if not member_response.data:
        return 0, None

    member = member_response.data[0]
    trainer_id = member['trainer_id']

    # Parse member creation date
    created_at = datetime.fromisoformat(member['created_at'].replace('Z', '+00:00'))
    member_month_start = created_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date()

    if member_month_start.month == 12:
        next_month = member_month_start.replace(year=member_month_start.year + 1, month=1)
    else:
        next_month = member_month_start.replace(month=member_month_start.month + 1)

    # Calculate what was paid (with this member)
    original_incentives, _ = calculate_trainer_incentives_for_month(
        trainer_id, member_month_start, next_month, exclude_member_id=None
    )

    # Calculate what should have been paid (without this member)
    adjusted_incentives, _ = calculate_trainer_incentives_for_month(
        trainer_id, member_month_start, next_month, exclude_member_id=member_id
    )

    # The difference is what needs to be deducted
    deduction = original_incentives - adjusted_incentives

    return deduction, member_month_start


@app.route('/members/<member_id>/refund', methods=['POST'])
@login_required
def refund_member(member_id):
    """Process a member refund with proportional calculation based on completed sessions"""
    user = session['user']

    # Admins and trainers can process refunds
    if user['role'] not in ['main_admin', 'branch_admin', 'trainer']:
        flash('환불 처리 권한이 없습니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    # Get member info
    member_response = supabase.table('members').select('*').eq('id', member_id).execute()
    if not member_response.data:
        flash('회원을 찾을 수 없습니다.', 'error')
        return redirect(url_for('members'))

    member = member_response.data[0]

    # Check if already refunded
    if member.get('refund_status') == 'refunded':
        flash('이미 환불 처리된 회원입니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    # Check trainer permission - can only refund own members
    if user['role'] == 'trainer':
        if member['trainer_id'] != user['id']:
            flash('본인 회원만 환불 처리할 수 있습니다.', 'error')
            return redirect(url_for('view_member', member_id=member_id))

    # Check branch_admin permission
    if user['role'] == 'branch_admin':
        trainer = supabase.table('users').select('branch_id').eq('id', member['trainer_id']).execute()
        if trainer.data and trainer.data[0]['branch_id'] != user['branch_id']:
            flash('환불 처리 권한이 없습니다.', 'error')
            return redirect(url_for('view_member', member_id=member_id))

    # Count completed sessions for this member
    completed_sessions_response = supabase.table('schedules').select('id').eq('member_id', member_id).eq('status', '수업 완료').execute()
    completed_sessions = len(completed_sessions_response.data) if completed_sessions_response.data else 0

    # Original values
    original_sessions = member['sessions']
    unit_price = member['unit_price']

    # Calculate proportional amounts
    # Original: sessions × unit_price (e.g., 10 sessions × 10,000 = 100,000)
    # Completed: completed_sessions × unit_price (e.g., 4 sessions × 10,000 = 40,000)
    # Refund: remaining_sessions × unit_price (e.g., 6 sessions × 10,000 = 60,000)
    original_amount = original_sessions * unit_price
    completed_amount = completed_sessions * unit_price
    refund_amount = original_amount - completed_amount
    remaining_sessions = original_sessions - completed_sessions

    # Determine current month
    current_month = datetime.now(KST).date().replace(day=1)

    # Check if member was created in current month
    created_at = datetime.fromisoformat(member['created_at'].replace('Z', '+00:00'))
    member_month = created_at.replace(day=1).date()

    is_same_month = (member_month.year == current_month.year and
                     member_month.month == current_month.month)

    # Calculate incentive deduction (only if not same month)
    deduction_amount = 0
    if not is_same_month and refund_amount > 0:
        deduction_amount, _ = calculate_refund_deduction(member_id)

    try:
        # Update member with refund info and proportional session count
        update_data = {
            'refund_status': 'refunded',
            'original_sessions': original_sessions,  # Store original sessions before refund
            'sessions': completed_sessions,  # Update sessions to completed only (매출 will reflect this)
            'refund_amount': int(deduction_amount) if not is_same_month else 0,
            'refund_sessions': remaining_sessions,  # Number of sessions refunded
            'refund_original_month': member_month.isoformat(),
            'refund_applied_month': current_month.isoformat(),
            'refunded_at': datetime.now(KST).isoformat(),
            'refunded_by': user['id']
        }

        supabase.table('members').update(update_data).eq('id', member_id).execute()

        if completed_sessions == 0:
            msg = f'회원 환불 처리가 완료되었습니다. (완료된 수업 없음 - 전액 환불)'
        else:
            msg = f'회원 환불 처리가 완료되었습니다. (완료: {completed_sessions}회, 환불: {remaining_sessions}회, 조정 매출: {completed_amount:,}원)'

        if not is_same_month and deduction_amount > 0:
            msg += f' (인센티브 차감: {int(deduction_amount):,}원)'

        flash(msg, 'success')

    except Exception as e:
        flash(f'환불 처리 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('view_member', member_id=member_id))


@app.route('/members/<member_id>/cancel-refund', methods=['POST'])
@login_required
def cancel_refund(member_id):
    """Cancel a member refund - Super Admin only"""
    user = session['user']

    # Only super admin can cancel refunds
    if user['role'] != 'main_admin':
        flash('환불 취소 권한이 없습니다. (최고관리자만 가능)', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    # Get member info
    member_response = supabase.table('members').select('*').eq('id', member_id).execute()
    if not member_response.data:
        flash('회원을 찾을 수 없습니다.', 'error')
        return redirect(url_for('members'))

    member = member_response.data[0]

    # Check if actually refunded
    if member.get('refund_status') != 'refunded':
        flash('환불 처리되지 않은 회원입니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    try:
        # Restore original sessions if available
        original_sessions = member.get('original_sessions')

        # Clear refund info and restore sessions
        update_data = {
            'refund_status': None,
            'refund_amount': None,
            'refund_sessions': None,
            'refund_original_month': None,
            'refund_applied_month': None,
            'refunded_at': None,
            'refunded_by': None,
            'original_sessions': None
        }

        # Restore original sessions if they were saved
        if original_sessions:
            update_data['sessions'] = original_sessions

        supabase.table('members').update(update_data).eq('id', member_id).execute()

        if original_sessions:
            flash(f'환불이 취소되었습니다. (수업 횟수 복원: {original_sessions}회)', 'success')
        else:
            flash('환불이 취소되었습니다.', 'success')

    except Exception as e:
        flash(f'환불 취소 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('view_member', member_id=member_id))


@app.route('/members/<member_id>/transfer', methods=['GET', 'POST'])
@login_required
def transfer_member(member_id):
    """Transfer a member to another trainer in the same branch (회원 인계)"""
    user = session['user']

    # Get member info
    member_response = supabase.table('members').select(
        '*, trainer:users!members_trainer_id_fkey(id, name, branch_id)'
    ).eq('id', member_id).execute()

    if not member_response.data:
        flash('회원을 찾을 수 없습니다.', 'error')
        return redirect(url_for('members'))

    member = member_response.data[0]

    # Check trainer permission - can only transfer own members
    if user['role'] == 'trainer':
        if member['trainer_id'] != user['id']:
            flash('본인 회원만 인계할 수 있습니다.', 'error')
            return redirect(url_for('view_member', member_id=member_id))

    # Check if already refunded or transferred
    if member.get('refund_status') == 'refunded':
        flash('환불 처리된 회원은 인계할 수 없습니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    if member.get('transfer_status') == 'transferred':
        flash('이미 인계된 회원입니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    # Get the trainer's branch
    from_trainer = member.get('trainer')
    if not from_trainer:
        flash('담당 트레이너 정보를 찾을 수 없습니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    branch_id = from_trainer.get('branch_id')

    # Check branch_admin permission
    if user['role'] == 'branch_admin' and user['branch_id'] != branch_id:
        flash('해당 지점의 회원만 인계할 수 있습니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    # Get other trainers in the same branch
    trainers_response = supabase.table('users').select('id, name').eq(
        'branch_id', branch_id
    ).eq('role', 'trainer').neq('id', member['trainer_id']).execute()

    available_trainers = trainers_response.data if trainers_response.data else []

    if request.method == 'GET':
        # Show transfer form
        return render_template('transfer_member.html',
                               user=user,
                               member=member,
                               trainers=available_trainers)

    # POST - Process the transfer
    new_trainer_id = request.form.get('new_trainer_id')
    if not new_trainer_id:
        flash('인계받을 트레이너를 선택해주세요.', 'error')
        return redirect(url_for('transfer_member', member_id=member_id))

    # Verify new trainer is in same branch
    new_trainer = next((t for t in available_trainers if t['id'] == new_trainer_id), None)
    if not new_trainer:
        flash('유효하지 않은 트레이너입니다.', 'error')
        return redirect(url_for('transfer_member', member_id=member_id))

    # Count completed sessions for this member
    completed_sessions_response = supabase.table('schedules').select('id').eq(
        'member_id', member_id
    ).eq('status', '수업 완료').execute()
    completed_sessions = len(completed_sessions_response.data) if completed_sessions_response.data else 0

    # Original values
    original_sessions = member['sessions']
    unit_price = member['unit_price']
    remaining_sessions = original_sessions - completed_sessions

    if remaining_sessions <= 0:
        flash('남은 세션이 없어 인계할 수 없습니다.', 'error')
        return redirect(url_for('view_member', member_id=member_id))

    # Calculate amounts
    completed_amount = completed_sessions * unit_price
    remaining_amount = remaining_sessions * unit_price

    current_month = datetime.now(KST).date().replace(day=1)

    try:
        # 1. Update original member record (similar to refund - proportional)
        original_update = {
            'transfer_status': 'transferred',
            'original_sessions': original_sessions,
            'sessions': completed_sessions,  # Keep only completed sessions for 매출
            'transferred_to': new_trainer_id,
            'transferred_sessions': remaining_sessions,
            'transferred_at': datetime.now(KST).isoformat(),
            'transferred_by': user['id']
        }
        supabase.table('members').update(original_update).eq('id', member_id).execute()

        # 2. Create new member record for the new trainer with remaining sessions
        new_member_data = {
            'member_name': member['member_name'],
            'phone': member['phone'],
            'payment_method': member['payment_method'],
            'sessions': remaining_sessions,
            'unit_price': unit_price,
            'channel': member['channel'],
            'trainer_id': new_trainer_id,
            'transfer_status': 'received',
            'transferred_from': member_id,
            'transferred_from_trainer': member['trainer_id'],
            'signature': member.get('signature')
        }
        new_member_response = supabase.table('members').insert(new_member_data).execute()
        new_member_id = new_member_response.data[0]['id'] if new_member_response.data else None

        # 3. Remove all future scheduled sessions (status='계획') for the original member
        today = datetime.now(KST).date().isoformat()
        deleted_schedules = supabase.table('schedules').delete().eq(
            'member_id', member_id
        ).eq('status', '계획').gte('date', today).execute()
        deleted_count = len(deleted_schedules.data) if deleted_schedules.data else 0

        transfer_msg = f'회원 인계가 완료되었습니다. ({from_trainer["name"]} → {new_trainer["name"]}, 완료: {completed_sessions}회, 인계: {remaining_sessions}회)'
        if deleted_count > 0:
            transfer_msg += f' 예정된 수업 {deleted_count}개가 삭제되었습니다.'
        flash(transfer_msg, 'success')

        if new_member_id:
            return redirect(url_for('view_member', member_id=new_member_id))
        return redirect(url_for('members'))

    except Exception as e:
        flash(f'회원 인계 중 오류가 발생했습니다: {str(e)}', 'error')
        return redirect(url_for('view_member', member_id=member_id))


def get_trainer_dayoffs(trainer_ids, month_str):
    """Get 휴무일 count for trainers for a specific month"""
    if not trainer_ids:
        return {}
    try:
        response = supabase.table('trainer_dayoffs').select('trainer_id, days').eq('month', month_str).in_('trainer_id', trainer_ids).execute()
        return {d['trainer_id']: d['days'] for d in (response.data or [])}
    except:
        return {}


def calculate_dayoff_deduction(days):
    """Calculate 휴무 deduction: first day free, then 33,000원 per day"""
    if days <= 1:
        return 0
    return (days - 1) * 33000


@app.route('/salary')
@login_required
def salary():
    user = session['user']

    # Get selected month (default to current month)
    month_str = request.args.get('month')
    if month_str:
        try:
            selected_date = datetime.strptime(month_str, '%Y-%m').date()
        except:
            selected_date = datetime.now(KST).date()
    else:
        selected_date = datetime.now(KST).date()

    # Calculate month range
    month_start = selected_date.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    # Calculate 6-month range (current month + past 5 months)
    six_month_start = month_start
    for _ in range(5):
        if six_month_start.month == 1:
            six_month_start = six_month_start.replace(year=six_month_start.year - 1, month=12)
        else:
            six_month_start = six_month_start.replace(month=six_month_start.month - 1)

    # Load salary settings from database (or use defaults)
    salary_settings = get_salary_settings()

    # Month string for 휴무 lookup
    month_key = month_start.strftime('%Y-%m')

    # Get branches and trainers based on role
    branches_list = []
    filter_branch_id = request.args.get('branch_id')
    trainer_data = []
    total_sales = 0
    total_incentive = 0

    if user['role'] == 'trainer':
        # Trainer sees only their own data - current month
        members_response = supabase.table('members').select('id, sessions, unit_price, channel, refund_status, created_at').eq('trainer_id', user['id']).gte('created_at', month_start.isoformat()).lt('created_at', next_month.isoformat()).execute()
        members_list = members_response.data if members_response.data else []

        # Get 6-month data for master trainer bonus
        six_month_response = supabase.table('members').select('sessions, unit_price, channel, refund_status').eq('trainer_id', user['id']).gte('created_at', six_month_start.isoformat()).lt('created_at', next_month.isoformat()).execute()
        six_month_members = six_month_response.data if six_month_response.data else []

        # Get all members for this trainer (for lesson fee calculation)
        all_members_response = supabase.table('members').select('id, unit_price').eq('trainer_id', user['id']).execute()
        all_members = {m['id']: m['unit_price'] for m in (all_members_response.data or [])}

        # Get completed schedules for this month
        schedules_response = supabase.table('schedules').select('member_id, work_type, status').eq('trainer_id', user['id']).eq('status', '수업 완료').gte('schedule_date', month_start.isoformat()).lt('schedule_date', next_month.isoformat()).execute()
        schedules_list = schedules_response.data if schedules_response.data else []

        # Get refund deductions applied to this month
        refund_response = supabase.table('members').select('refund_amount').eq('trainer_id', user['id']).eq('refund_status', 'refunded').eq('refund_applied_month', month_start.isoformat()).execute()
        refund_deductions = sum(m.get('refund_amount', 0) or 0 for m in (refund_response.data or []))

        # Calculate sales with 50% for WI channel (refunded members included with proportional amount)
        sales = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in members_list
        )
        six_month_sales = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in six_month_members
        )
        incentive = calculate_incentive(sales, salary_settings)
        master_bonus = calculate_master_trainer_bonus(six_month_sales, salary_settings)

        # Calculate lesson fees
        lesson_fee_base_main = 0
        lesson_fee_base_other = 0
        for schedule in schedules_list:
            member_unit_price = all_members.get(schedule['member_id'], 0)
            if schedule['work_type'] == '근무내':
                lesson_fee_base_main += member_unit_price
            else:
                lesson_fee_base_other += member_unit_price

        lesson_fee_rate_main = calculate_lesson_fee_rate(sales, salary_settings)
        lesson_fee_rate_other = calculate_lesson_fee_rate_other(sales, salary_settings)
        lesson_fee_main = int(lesson_fee_base_main * lesson_fee_rate_main / 100)
        lesson_fee_other = int(lesson_fee_base_other * lesson_fee_rate_other / 100)

        # Get 휴무일 for trainer
        trainer_dayoffs = get_trainer_dayoffs([user['id']], month_key)
        dayoff_days = trainer_dayoffs.get(user['id'], 0)
        dayoff_deduction = calculate_dayoff_deduction(dayoff_days)

        # Calculate class count and class incentive (수업당 인센)
        class_count = len(schedules_list)
        class_incentive = calculate_class_incentive(class_count, sales)

        # Calculate OT incentive
        ot_session_count, ot_incentive = calculate_ot_incentive(user['id'], month_start, next_month)

        # Get salary adjustments for this trainer
        adjustments_response = supabase.table('salary_adjustments').select('*').eq('trainer_id', user['id']).eq('month', month_key).order('created_at').execute()
        adjustments = adjustments_response.data if adjustments_response.data else []
        adjustment_total = sum(a.get('amount', 0) for a in adjustments)

        total_salary = incentive + class_incentive + master_bonus + lesson_fee_main + lesson_fee_other + ot_incentive + adjustment_total - int(refund_deductions) - dayoff_deduction

        trainer_data.append({
            'id': user['id'],
            'name': user['name'],
            'branch': '-',
            'sales': sales,
            'six_month_sales': six_month_sales,
            'incentive': incentive,
            'class_count': class_count,
            'class_incentive': class_incentive,
            'ot_session_count': ot_session_count,
            'ot_incentive': ot_incentive,
            'master_bonus': master_bonus,
            'lesson_fee_base_main': lesson_fee_base_main,
            'lesson_fee_base_other': lesson_fee_base_other,
            'lesson_fee_rate_main': lesson_fee_rate_main,
            'lesson_fee_rate_other': lesson_fee_rate_other,
            'lesson_fee_main': lesson_fee_main,
            'lesson_fee_other': lesson_fee_other,
            'refund_deduction': int(refund_deductions),
            'dayoff_days': dayoff_days,
            'dayoff_deduction': dayoff_deduction,
            'adjustments': adjustments,
            'adjustment_total': adjustment_total,
            'total_salary': total_salary
        })
        total_sales = sales
        total_incentive = total_salary

    else:
        # Admin views
        if user['role'] == 'main_admin':
            branches_response = supabase.table('branches').select('*').order('name').execute()
            branches_list = branches_response.data if branches_response.data else []

            # Get trainers with optional branch filter
            if filter_branch_id:
                trainers_response = supabase.table('users').select('*, branch:branches(name)').eq('role', 'trainer').eq('branch_id', filter_branch_id).order('name').execute()
            else:
                trainers_response = supabase.table('users').select('*, branch:branches(name)').eq('role', 'trainer').order('name').execute()
        else:
            # branch_admin - only see their branch trainers
            trainers_response = supabase.table('users').select('*, branch:branches(name)').eq('role', 'trainer').eq('branch_id', user['branch_id']).order('name').execute()

        trainers_list = trainers_response.data if trainers_response.data else []
        trainer_ids = [t['id'] for t in trainers_list]

        # Get all members created in the selected month for these trainers
        if trainer_ids:
            members_response = supabase.table('members').select('id, trainer_id, sessions, unit_price, channel, refund_status, created_at').in_('trainer_id', trainer_ids).gte('created_at', month_start.isoformat()).lt('created_at', next_month.isoformat()).execute()
            members_list = members_response.data if members_response.data else []

            # Get 6-month data for master trainer bonus
            six_month_response = supabase.table('members').select('trainer_id, sessions, unit_price, channel, refund_status').in_('trainer_id', trainer_ids).gte('created_at', six_month_start.isoformat()).lt('created_at', next_month.isoformat()).execute()
            six_month_members = six_month_response.data if six_month_response.data else []

            # Get all members for these trainers (for lesson fee calculation)
            all_members_response = supabase.table('members').select('id, trainer_id, unit_price').in_('trainer_id', trainer_ids).execute()
            all_members_list = all_members_response.data if all_members_response.data else []

            # Get completed schedules for this month
            schedules_response = supabase.table('schedules').select('trainer_id, member_id, work_type').in_('trainer_id', trainer_ids).eq('status', '수업 완료').gte('schedule_date', month_start.isoformat()).lt('schedule_date', next_month.isoformat()).execute()
            schedules_list = schedules_response.data if schedules_response.data else []

            # Get refund deductions applied to this month for each trainer
            refund_response = supabase.table('members').select('trainer_id, refund_amount').in_('trainer_id', trainer_ids).eq('refund_status', 'refunded').eq('refund_applied_month', month_start.isoformat()).execute()
            trainer_refund_deductions = {}
            for r in (refund_response.data or []):
                tid = r['trainer_id']
                trainer_refund_deductions[tid] = trainer_refund_deductions.get(tid, 0) + (r.get('refund_amount', 0) or 0)
        else:
            members_list = []
            six_month_members = []
            all_members_list = []
            schedules_list = []
            trainer_refund_deductions = {}

        # Build member unit_price lookup by trainer
        trainer_member_prices = {}
        for m in all_members_list:
            tid = m['trainer_id']
            if tid not in trainer_member_prices:
                trainer_member_prices[tid] = {}
            trainer_member_prices[tid][m['id']] = m['unit_price']

        # Calculate sales (매출) per trainer - current month (50% for WI, refunded included with proportional amount)
        trainer_sales = {}
        for member in members_list:
            tid = member['trainer_id']
            contract_amount = member['sessions'] * member['unit_price']
            # Apply 50% if channel is WI
            if member.get('channel') == 'WI':
                contract_amount = contract_amount * 0.5
            trainer_sales[tid] = trainer_sales.get(tid, 0) + contract_amount

        # Calculate 6-month sales per trainer (50% for WI, refunded included with proportional amount)
        trainer_six_month_sales = {}
        for member in six_month_members:
            tid = member['trainer_id']
            contract_amount = member['sessions'] * member['unit_price']
            # Apply 50% if channel is WI
            if member.get('channel') == 'WI':
                contract_amount = contract_amount * 0.5
            trainer_six_month_sales[tid] = trainer_six_month_sales.get(tid, 0) + contract_amount

        # Calculate lesson fee base per trainer and count classes
        trainer_lesson_fees = {}
        trainer_class_counts = {}
        for schedule in schedules_list:
            tid = schedule['trainer_id']
            member_id = schedule['member_id']
            work_type = schedule['work_type']
            unit_price = trainer_member_prices.get(tid, {}).get(member_id, 0)

            if tid not in trainer_lesson_fees:
                trainer_lesson_fees[tid] = {'main': 0, 'other': 0}

            if work_type == '근무내':
                trainer_lesson_fees[tid]['main'] += unit_price
            else:
                trainer_lesson_fees[tid]['other'] += unit_price

            # Count classes per trainer
            trainer_class_counts[tid] = trainer_class_counts.get(tid, 0) + 1

        # Get 휴무일 for all trainers
        all_dayoffs = get_trainer_dayoffs(trainer_ids, month_key) if trainer_ids else {}

        # Get salary adjustments for all trainers in this month
        trainer_adjustments = {}
        if trainer_ids:
            adjustments_response = supabase.table('salary_adjustments').select('*').in_('trainer_id', trainer_ids).eq('month', month_key).order('created_at').execute()
            for adj in (adjustments_response.data or []):
                tid = adj['trainer_id']
                if tid not in trainer_adjustments:
                    trainer_adjustments[tid] = []
                trainer_adjustments[tid].append(adj)

        # Build trainer data with sales and incentive
        for trainer in trainers_list:
            sales = trainer_sales.get(trainer['id'], 0)
            six_month_sales = trainer_six_month_sales.get(trainer['id'], 0)
            incentive = calculate_incentive(sales, salary_settings)
            master_bonus = calculate_master_trainer_bonus(six_month_sales, salary_settings)

            # Lesson fees
            lesson_data = trainer_lesson_fees.get(trainer['id'], {'main': 0, 'other': 0})
            lesson_fee_base_main = lesson_data['main']
            lesson_fee_base_other = lesson_data['other']
            lesson_fee_rate_main = calculate_lesson_fee_rate(sales, salary_settings)
            lesson_fee_rate_other = calculate_lesson_fee_rate_other(sales, salary_settings)
            lesson_fee_main = int(lesson_fee_base_main * lesson_fee_rate_main / 100)
            lesson_fee_other = int(lesson_fee_base_other * lesson_fee_rate_other / 100)

            # Refund deductions
            refund_deduction = int(trainer_refund_deductions.get(trainer['id'], 0))

            # 휴무 deduction
            dayoff_days = all_dayoffs.get(trainer['id'], 0)
            dayoff_deduction = calculate_dayoff_deduction(dayoff_days)

            # Calculate class count and class incentive (수업당 인센)
            class_count = trainer_class_counts.get(trainer['id'], 0)
            class_incentive = calculate_class_incentive(class_count, sales)

            # Calculate OT incentive
            ot_session_count, ot_incentive = calculate_ot_incentive(trainer['id'], month_start, next_month)

            # Get salary adjustments for this trainer
            adjustments = trainer_adjustments.get(trainer['id'], [])
            adjustment_total = sum(a.get('amount', 0) for a in adjustments)

            trainer_total = incentive + class_incentive + master_bonus + lesson_fee_main + lesson_fee_other + ot_incentive + adjustment_total - refund_deduction - dayoff_deduction
            total_sales += sales
            total_incentive += trainer_total

            trainer_data.append({
                'id': trainer['id'],
                'name': trainer['name'],
                'branch': trainer['branch']['name'] if trainer.get('branch') else '-',
                'sales': sales,
                'six_month_sales': six_month_sales,
                'incentive': incentive,
                'class_count': class_count,
                'class_incentive': class_incentive,
                'ot_session_count': ot_session_count,
                'ot_incentive': ot_incentive,
                'master_bonus': master_bonus,
                'lesson_fee_base_main': lesson_fee_base_main,
                'lesson_fee_base_other': lesson_fee_base_other,
                'lesson_fee_rate_main': lesson_fee_rate_main,
                'lesson_fee_rate_other': lesson_fee_rate_other,
                'lesson_fee_main': lesson_fee_main,
                'lesson_fee_other': lesson_fee_other,
                'refund_deduction': refund_deduction,
                'dayoff_days': dayoff_days,
                'dayoff_deduction': dayoff_deduction,
                'adjustments': adjustments,
                'adjustment_total': adjustment_total,
                'total_salary': trainer_total
            })

        # Sort by name for dropdown
        trainer_data.sort(key=lambda x: x['name'])

    # Get selected trainer for admin/manager view
    selected_trainer_id = request.args.get('trainer_id')
    selected_trainer = None
    if selected_trainer_id and user['role'] != 'trainer':
        for t in trainer_data:
            if t['id'] == selected_trainer_id:
                selected_trainer = t
                break

    return render_template('salary.html',
                         user=user,
                         trainers=trainer_data,
                         branches=branches_list,
                         filter_branch_id=filter_branch_id,
                         selected_trainer_id=selected_trainer_id,
                         selected_trainer=selected_trainer,
                         selected_month=month_start.strftime('%Y-%m'),
                         total_sales=total_sales,
                         total_incentive=total_incentive,
                         salary_settings=salary_settings)


@app.route('/salary/dayoff/update', methods=['POST'])
@login_required
def update_trainer_dayoff():
    """API endpoint to update trainer's 휴무일 count - Admin/Manager only"""
    user = session['user']

    # Only admin and branch_admin can update dayoffs
    if user['role'] not in ['main_admin', 'branch_admin']:
        return jsonify({'success': False, 'error': '권한이 없습니다.'}), 403

    data = request.get_json()
    trainer_id = data.get('trainer_id')
    month = data.get('month')  # Format: YYYY-MM
    days = data.get('days', 0)

    if not trainer_id or not month:
        return jsonify({'success': False, 'error': '필수 정보가 누락되었습니다.'}), 400

    try:
        days = int(days)
        if days < 0:
            days = 0
    except:
        days = 0

    try:
        # Check if branch_admin has permission for this trainer
        if user['role'] == 'branch_admin':
            trainer = supabase.table('users').select('branch_id').eq('id', trainer_id).execute()
            if not trainer.data or trainer.data[0].get('branch_id') != user['branch_id']:
                return jsonify({'success': False, 'error': '이 트레이너의 휴무를 수정할 권한이 없습니다.'}), 403

        # Check if record exists
        existing = supabase.table('trainer_dayoffs').select('id').eq('trainer_id', trainer_id).eq('month', month).execute()

        if days == 0:
            # Delete record if days is 0
            if existing.data:
                supabase.table('trainer_dayoffs').delete().eq('id', existing.data[0]['id']).execute()
        elif existing.data:
            # Update existing record
            supabase.table('trainer_dayoffs').update({
                'days': days,
                'updated_by': user['id']
            }).eq('id', existing.data[0]['id']).execute()
        else:
            # Insert new record
            supabase.table('trainer_dayoffs').insert({
                'trainer_id': trainer_id,
                'month': month,
                'days': days,
                'updated_by': user['id']
            }).execute()

        # Calculate the new deduction
        deduction = calculate_dayoff_deduction(days)

        return jsonify({'success': True, 'days': days, 'deduction': deduction})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Salary adjustment routes - Main Admin only
@app.route('/salary/adjustment/add', methods=['POST'])
@role_required('main_admin')
def add_salary_adjustment():
    user = session['user']
    try:
        data = request.get_json()
        trainer_id = data.get('trainer_id')
        month = data.get('month')
        amount = int(data.get('amount', 0))
        memo = data.get('memo', '').strip()

        if not all([trainer_id, month, memo]) or amount == 0:
            return jsonify({'success': False, 'error': '모든 필수 항목을 입력해주세요.'}), 400

        # Insert adjustment
        supabase.table('salary_adjustments').insert({
            'trainer_id': trainer_id,
            'month': month,
            'amount': amount,
            'memo': memo,
            'created_by': user['id']
        }).execute()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/salary/adjustment/delete/<adjustment_id>', methods=['POST'])
@role_required('main_admin')
def delete_salary_adjustment(adjustment_id):
    try:
        supabase.table('salary_adjustments').delete().eq('id', adjustment_id).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Password change route - for all users
@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    user = session['user']

    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not all([current_password, new_password, confirm_password]):
            flash('모든 항목을 입력해주세요.', 'error')
            return render_template('change_password.html', user=user)

        if new_password != confirm_password:
            flash('새 비밀번호가 일치하지 않습니다.', 'error')
            return render_template('change_password.html', user=user)

        if len(new_password) < 4:
            flash('비밀번호는 4자 이상이어야 합니다.', 'error')
            return render_template('change_password.html', user=user)

        # Verify current password
        user_response = supabase.table('users').select('password_hash').eq('id', user['id']).execute()
        if not user_response.data or user_response.data[0]['password_hash'] != current_password:
            flash('현재 비밀번호가 올바르지 않습니다.', 'error')
            return render_template('change_password.html', user=user)

        try:
            supabase.table('users').update({'password_hash': new_password}).eq('id', user['id']).execute()
            flash('비밀번호가 성공적으로 변경되었습니다.', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f'비밀번호 변경 중 오류가 발생했습니다: {str(e)}', 'error')

    return render_template('change_password.html', user=user)


# Toggle user status (activate/deactivate) - Main Admin only
@app.route('/users/<user_id>/toggle-status', methods=['POST'])
@role_required('main_admin')
def toggle_user_status(user_id):
    try:
        # Get current user status
        user_response = supabase.table('users').select('status, name, role').eq('id', user_id).execute()
        if not user_response.data:
            flash('사용자를 찾을 수 없습니다.', 'error')
            return redirect(request.referrer or url_for('dashboard'))

        target_user = user_response.data[0]

        # Don't allow deactivating main_admin
        if target_user['role'] == 'main_admin':
            flash('총관리자는 비활성화할 수 없습니다.', 'error')
            return redirect(request.referrer or url_for('dashboard'))

        current_status = target_user.get('status', '활성화')
        new_status = '비활성화' if current_status == '활성화' else '활성화'

        supabase.table('users').update({'status': new_status}).eq('id', user_id).execute()
        flash(f'{target_user["name"]}님이 {new_status} 되었습니다.', 'success')

    except Exception as e:
        flash(f'상태 변경 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(request.referrer or url_for('dashboard'))


# Delete user - Main Admin only
@app.route('/users/<user_id>/delete', methods=['POST'])
@role_required('main_admin')
def delete_user(user_id):
    try:
        # Get user info first
        user_response = supabase.table('users').select('name, role').eq('id', user_id).execute()
        if not user_response.data:
            flash('사용자를 찾을 수 없습니다.', 'error')
            return redirect(request.referrer or url_for('dashboard'))

        target_user = user_response.data[0]

        # Don't allow deleting main_admin
        if target_user['role'] == 'main_admin':
            flash('총관리자는 삭제할 수 없습니다.', 'error')
            return redirect(request.referrer or url_for('dashboard'))

        # Delete the user
        supabase.table('users').delete().eq('id', user_id).execute()
        flash(f'{target_user["name"]}님이 삭제되었습니다.', 'success')

    except Exception as e:
        flash(f'사용자 삭제 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(request.referrer or url_for('dashboard'))


# ==================== OT MEMBER MANAGEMENT ====================

def check_and_return_expired_ot_members(branch_id=None):
    """
    Check for OT assignments past deadline and return them to branch admin pool.
    Uses ot_assignments table for individual session tracking.
    Called on page load of relevant pages.
    """
    now = datetime.now(KST)

    try:
        # Find expired OT assignments (status='assigned' and deadline passed)
        expired_assignments = supabase.table('ot_assignments').select(
            '*, member:members!ot_assignments_member_id_fkey(id, member_name, branch_id)'
        ).eq('status', 'assigned').lt('deadline', now.isoformat()).execute().data or []

        for assignment in expired_assignments:
            # Check if there's a completed schedule for this assignment
            schedules_response = supabase.table('schedules').select('id, status').eq(
                'member_id', assignment['member_id']
            ).eq('trainer_id', assignment['trainer_id']).execute()

            completed = False
            for sch in (schedules_response.data or []):
                if sch['status'] == '수업 완료':
                    completed = True
                    break

            if not completed:
                # Not completed - return this assignment to pool
                supabase.table('ot_assignments').update({
                    'status': 'returned',
                    'returned_at': now.isoformat()
                }).eq('id', assignment['id']).execute()

                # Update member's remaining sessions
                member_response = supabase.table('members').select('ot_remaining_sessions, ot_status').eq(
                    'id', assignment['member_id']
                ).execute()

                if member_response.data:
                    member = member_response.data[0]
                    new_remaining = (member.get('ot_remaining_sessions') or 0) + 1

                    # Check if there are still other active assignments
                    other_assignments = supabase.table('ot_assignments').select('id').eq(
                        'member_id', assignment['member_id']
                    ).eq('status', 'assigned').execute()

                    new_status = 'partial' if (other_assignments.data and len(other_assignments.data) > 0) else 'unassigned'
                    if new_remaining >= member_response.data[0].get('sessions', 1):
                        new_status = 'unassigned'

                    supabase.table('members').update({
                        'ot_remaining_sessions': new_remaining,
                        'ot_status': new_status
                    }).eq('id', assignment['member_id']).execute()

                # Record history
                supabase.table('ot_assignment_history').insert({
                    'member_id': assignment['member_id'],
                    'trainer_id': assignment['trainer_id'],
                    'action': 'returned',
                    'notes': f'{assignment["session_number"]}차 기한 만료로 자동 반환'
                }).execute()
    except Exception as e:
        print(f"Error in check_and_return_expired_ot_members: {e}")


def check_ot_session_completion(member_id):
    """
    Check if OT member has completed all sessions and update status accordingly.
    Called after completing a session.
    """
    try:
        # Get member info
        member_response = supabase.table('members').select(
            'id, sessions, member_type, ot_status'
        ).eq('id', member_id).execute()

        if not member_response.data:
            return

        member = member_response.data[0]

        # Only check OT members that are in active states
        if member.get('member_type') != 'OT회원':
            return

        if member.get('ot_status') not in ['assigned', 'partial', None]:
            return

        # Count completed OT assignments (more reliable than counting schedules)
        assignments_response = supabase.table('ot_assignments').select('id, status').eq(
            'member_id', member_id
        ).execute()

        assignments = assignments_response.data if assignments_response.data else []
        completed_count = len([a for a in assignments if a['status'] == 'completed'])
        total_assignments = len(assignments)

        # Check if all assignments are completed
        if completed_count >= member['sessions'] and completed_count > 0:
            # All sessions completed
            supabase.table('members').update({
                'ot_status': 'completed'
            }).eq('id', member_id).execute()

            # Record history
            supabase.table('ot_assignment_history').insert({
                'member_id': member_id,
                'action': 'all_completed',
                'notes': f'모든 세션 완료 ({completed_count}/{member["sessions"]})'
            }).execute()
        elif completed_count > 0 and total_assignments < member['sessions']:
            # Some completed, more can be assigned
            supabase.table('members').update({
                'ot_status': 'partial'
            }).eq('id', member_id).execute()
    except Exception as e:
        print(f"Error in check_ot_session_completion: {e}")


def calculate_ot_incentive(trainer_id, month_start, next_month):
    """
    Calculate OT incentive for a trainer.
    If trainer completes >10 OT sessions in a month, they get 5,000원 per OT session.
    Returns (ot_session_count, ot_incentive_amount)
    """
    try:
        # Get all OT member IDs
        ot_members_response = supabase.table('members').select('id').eq(
            'member_type', 'OT회원'
        ).execute()
        ot_member_ids = [m['id'] for m in (ot_members_response.data or [])]

        if not ot_member_ids:
            return 0, 0

        # Count completed OT sessions for this trainer in this month
        ot_session_count = 0
        for member_id in ot_member_ids:
            schedules_response = supabase.table('schedules').select('id').eq(
                'trainer_id', trainer_id
            ).eq('member_id', member_id).eq('status', '수업 완료').gte(
                'schedule_date', month_start.strftime('%Y-%m-%d')
            ).lt('schedule_date', next_month.strftime('%Y-%m-%d')).execute()

            ot_session_count += len(schedules_response.data) if schedules_response.data else 0

        # Calculate incentive (10개 이상부터 1개당 5,000원)
        if ot_session_count >= 10:
            ot_incentive = ot_session_count * 5000
        else:
            ot_incentive = 0

        return ot_session_count, ot_incentive
    except Exception as e:
        print(f"Error in calculate_ot_incentive: {e}")
        return 0, 0


def get_ot_session_number(member_id):
    """
    Get the current OT session number (how many completed + 1 for next session).
    """
    try:
        completed_response = supabase.table('schedules').select('id').eq(
            'member_id', member_id
        ).eq('status', '수업 완료').execute()

        completed_count = len(completed_response.data) if completed_response.data else 0
        return completed_count + 1
    except:
        return 1


# OT Members Management Page (Branch Admin)
@app.route('/ot-members')
@role_required('main_admin', 'branch_admin')
def ot_members():
    user = session['user']

    # Auto-return expired OT assignments
    check_and_return_expired_ot_members()

    # Get trainers for assignment dropdown
    if user['role'] == 'main_admin':
        trainers_response = supabase.table('users').select('id, name, branch_id').eq('role', 'trainer').execute()
        branches_response = supabase.table('branches').select('id, name').execute()
        branches = branches_response.data if branches_response.data else []
    else:
        trainers_response = supabase.table('users').select('id, name').eq('role', 'trainer').eq('branch_id', user['branch_id']).execute()
        branches = []

    trainers = trainers_response.data if trainers_response.data else []

    # Get filter
    filter_status = request.args.get('status', 'all')
    filter_branch_id = request.args.get('branch_id', '')

    # Build query for OT members
    query = supabase.table('members').select('*').eq('member_type', 'OT회원')

    # Filter by status
    if filter_status == 'unassigned':
        query = query.in_('ot_status', ['unassigned', 'partial'])
    elif filter_status == 'assigned':
        query = query.eq('ot_status', 'assigned')
    elif filter_status == 'completed':
        query = query.eq('ot_status', 'completed')

    ot_members_data = query.order('created_at', desc=True).execute().data or []

    # Filter by branch for branch_admin
    if user['role'] == 'branch_admin':
        ot_members_data = [m for m in ot_members_data if m.get('branch_id') == user['branch_id']]
    elif filter_branch_id:
        ot_members_data = [m for m in ot_members_data if m.get('branch_id') == filter_branch_id]

    # Get all OT assignments for these members
    member_ids = [m['id'] for m in ot_members_data]
    all_assignments = []
    if member_ids:
        assignments_response = supabase.table('ot_assignments').select(
            '*, trainer:users!ot_assignments_trainer_id_fkey(id, name)'
        ).in_('member_id', member_ids).order('session_number').execute()
        all_assignments = assignments_response.data if assignments_response.data else []

    # Add assignment info to each member
    now = datetime.now(KST)
    for member in ot_members_data:
        member['assignments'] = [a for a in all_assignments if a['member_id'] == member['id']]

        # Count completed sessions from assignments
        completed_count = len([a for a in member['assignments'] if a['status'] == 'completed'])
        member['completed_sessions'] = completed_count

        # Calculate remaining to assign: total sessions - number of assignments created
        total_sessions = member.get('sessions', 1)
        assigned_count = len(member['assignments'])
        member['remaining_to_assign'] = max(0, total_sessions - assigned_count)

        # Get next session number
        member['next_session_number'] = assigned_count + 1

        # Calculate earliest deadline from active assignments (assigned or scheduled)
        active_assignments = [a for a in member['assignments'] if a['status'] in ['assigned', 'scheduled']]
        if active_assignments:
            deadlines = []
            for a in active_assignments:
                if a.get('deadline'):
                    deadlines.append(parse_datetime(a['deadline']))
            if deadlines:
                earliest_deadline = min(deadlines)
                member['days_remaining'] = (earliest_deadline.date() - now.date()).days
            else:
                member['days_remaining'] = None
        else:
            member['days_remaining'] = None

    return render_template('ot_members.html',
                         user=user,
                         ot_members=ot_members_data,
                         trainers=trainers,
                         branches=branches,
                         filter_status=filter_status,
                         filter_branch_id=filter_branch_id)


# Assign OT Member to Trainer
@app.route('/ot-members/<member_id>/assign', methods=['POST'])
@role_required('main_admin', 'branch_admin')
def assign_ot_member(member_id):
    user = session['user']
    trainer_id = request.form.get('trainer_id')
    assign_sessions = request.form.get('assign_sessions', '1')

    if not trainer_id:
        flash('트레이너를 선택해주세요.', 'error')
        return redirect(url_for('ot_members'))

    try:
        assign_sessions = int(assign_sessions)
    except:
        assign_sessions = 1

    try:
        # Get member info
        member_response = supabase.table('members').select('*').eq('id', member_id).execute()
        if not member_response.data:
            flash('회원을 찾을 수 없습니다.', 'error')
            return redirect(url_for('ot_members'))

        member = member_response.data[0]

        # Verify OT member
        if member.get('member_type') != 'OT회원':
            flash('OT 회원만 배정할 수 있습니다.', 'error')
            return redirect(url_for('ot_members'))

        # Check remaining sessions
        remaining = member.get('ot_remaining_sessions', member.get('sessions', 1))
        if remaining <= 0:
            flash('배정 가능한 세션이 없습니다.', 'error')
            return redirect(url_for('ot_members'))

        if assign_sessions > remaining:
            assign_sessions = remaining

        # Get current assignment count to determine session numbers
        existing_assignments = supabase.table('ot_assignments').select('id').eq('member_id', member_id).execute()
        current_count = len(existing_assignments.data) if existing_assignments.data else 0

        # Calculate deadline (7 days from now)
        now = datetime.now(KST)
        deadline = now + timedelta(days=7)

        # Get trainer name for message
        trainer_response = supabase.table('users').select('name').eq('id', trainer_id).execute()
        trainer_name = trainer_response.data[0]['name'] if trainer_response.data else '트레이너'

        # Create ot_assignments records (one for each session)
        for i in range(assign_sessions):
            session_number = current_count + i + 1
            supabase.table('ot_assignments').insert({
                'member_id': member_id,
                'trainer_id': trainer_id,
                'session_number': session_number,
                'status': 'assigned',
                'assigned_at': now.isoformat(),
                'deadline': deadline.isoformat(),
                'extended': False
            }).execute()

        # Update member's remaining sessions and status
        new_remaining = remaining - assign_sessions
        new_status = 'assigned' if new_remaining == 0 else 'partial'

        supabase.table('members').update({
            'ot_remaining_sessions': new_remaining,
            'ot_status': new_status
        }).eq('id', member_id).execute()

        # Record history
        supabase.table('ot_assignment_history').insert({
            'member_id': member_id,
            'trainer_id': trainer_id,
            'action': 'assigned',
            'action_by': user['id'],
            'notes': f'{trainer_name}에게 {assign_sessions}회 배정 (기한: {deadline.strftime("%Y-%m-%d")})'
        }).execute()

        flash(f'{member["member_name"]}님 {assign_sessions}회가 {trainer_name}에게 배정되었습니다.', 'success')
    except Exception as e:
        flash(f'배정 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('ot_members'))


# Extend OT Deadline
@app.route('/ot-members/<member_id>/extend', methods=['POST'])
@role_required('main_admin', 'branch_admin')
def extend_ot_deadline(member_id):
    user = session['user']

    try:
        # Get member info
        member_response = supabase.table('members').select('*').eq('id', member_id).execute()
        if not member_response.data:
            flash('회원을 찾을 수 없습니다.', 'error')
            return redirect(url_for('ot_members'))

        member = member_response.data[0]

        # Check if already extended
        if member.get('ot_extended'):
            flash('이미 연장된 회원입니다. 연장은 1회만 가능합니다.', 'error')
            return redirect(url_for('ot_members'))

        # Check status
        if member.get('ot_status') != 'assigned':
            flash('배정된 회원만 연장할 수 있습니다.', 'error')
            return redirect(url_for('ot_members'))

        # Calculate new deadline
        current_deadline = parse_datetime(member['ot_deadline'])
        new_deadline = current_deadline + timedelta(days=7)

        # Update member
        supabase.table('members').update({
            'ot_deadline': new_deadline.isoformat(),
            'ot_extended': True
        }).eq('id', member_id).execute()

        # Record history
        supabase.table('ot_assignment_history').insert({
            'member_id': member_id,
            'trainer_id': member.get('ot_assigned_trainer_id'),
            'action': 'extended',
            'action_by': user['id'],
            'notes': f'기한 연장: {current_deadline.strftime("%Y-%m-%d")} → {new_deadline.strftime("%Y-%m-%d")}'
        }).execute()

        flash(f'{member["member_name"]}님의 기한이 {new_deadline.strftime("%Y-%m-%d")}까지 연장되었습니다.', 'success')
    except Exception as e:
        flash(f'연장 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('ot_members'))


# Manually Reclaim OT Member
@app.route('/ot-members/<member_id>/reclaim', methods=['POST'])
@role_required('main_admin', 'branch_admin')
def reclaim_ot_member(member_id):
    user = session['user']

    try:
        # Get member info
        member_response = supabase.table('members').select('*').eq('id', member_id).execute()
        if not member_response.data:
            flash('회원을 찾을 수 없습니다.', 'error')
            return redirect(url_for('ot_members'))

        member = member_response.data[0]

        if member.get('ot_status') not in ['assigned', 'partial']:
            flash('배정된 회원만 회수할 수 있습니다.', 'error')
            return redirect(url_for('ot_members'))

        now = datetime.now(KST)

        # Return all active assignments for this member
        active_assignments = supabase.table('ot_assignments').select('id, trainer_id, session_number').eq(
            'member_id', member_id
        ).eq('status', 'assigned').execute().data or []

        returned_count = 0
        for assignment in active_assignments:
            supabase.table('ot_assignments').update({
                'status': 'returned',
                'returned_at': now.isoformat()
            }).eq('id', assignment['id']).execute()
            returned_count += 1

        # Update member status
        supabase.table('members').update({
            'ot_status': 'unassigned',
            'ot_remaining_sessions': member.get('sessions', 1)
        }).eq('id', member_id).execute()

        # Record history
        supabase.table('ot_assignment_history').insert({
            'member_id': member_id,
            'action': 'returned',
            'action_by': user['id'],
            'notes': f'지점장에 의해 수동 회수 ({returned_count}회)'
        }).execute()

        flash(f'{member["member_name"]}님이 회수되었습니다.', 'success')
    except Exception as e:
        flash(f'회수 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('ot_members'))


# Trainer Extension Request (AJAX)
@app.route('/ot-assignments/<assignment_id>/extend', methods=['POST'])
@login_required
def extend_ot_assignment(assignment_id):
    """AJAX endpoint for trainers to extend their OT assignment deadline"""
    user = session['user']

    try:
        # Get assignment
        assignment_response = supabase.table('ot_assignments').select(
            '*, member:members!ot_assignments_member_id_fkey(member_name)'
        ).eq('id', assignment_id).execute()

        if not assignment_response.data:
            return jsonify({'success': False, 'error': '배정을 찾을 수 없습니다.'}), 404

        assignment = assignment_response.data[0]

        # Check ownership (trainer can only extend their own)
        if user['role'] == 'trainer' and assignment['trainer_id'] != user['id']:
            return jsonify({'success': False, 'error': '본인의 배정만 연장할 수 있습니다.'}), 403

        # Check if already extended
        if assignment.get('extended'):
            return jsonify({'success': False, 'error': '이미 연장된 배정입니다. 연장은 1회만 가능합니다.'}), 400

        # Check status
        if assignment['status'] != 'assigned':
            return jsonify({'success': False, 'error': '배정 상태가 아닙니다.'}), 400

        # Calculate new deadline (7 days from current deadline)
        current_deadline = parse_datetime(assignment['deadline'])
        new_deadline = current_deadline + timedelta(days=7)

        # Update assignment
        supabase.table('ot_assignments').update({
            'deadline': new_deadline.isoformat(),
            'extended': True
        }).eq('id', assignment_id).execute()

        # Record history
        supabase.table('ot_assignment_history').insert({
            'member_id': assignment['member_id'],
            'trainer_id': assignment['trainer_id'],
            'action': 'extended',
            'action_by': user['id'],
            'notes': f'{assignment["session_number"]}차 기한 연장: {current_deadline.strftime("%Y-%m-%d")} → {new_deadline.strftime("%Y-%m-%d")}'
        }).execute()

        member_name = assignment['member']['member_name'] if assignment.get('member') else 'OT 회원'
        return jsonify({
            'success': True,
            'message': f'{member_name}님의 기한이 {new_deadline.strftime("%Y-%m-%d")}까지 연장되었습니다.',
            'new_deadline': new_deadline.strftime('%Y-%m-%d')
        })

    except Exception as e:
        return jsonify({'success': False, 'error': f'연장 중 오류: {str(e)}'}), 500


# OT History Data (AJAX)
@app.route('/ot-history')
@role_required('main_admin', 'branch_admin')
def ot_history():
    """Get OT member history (completed and returned)"""
    user = session['user']

    try:
        # Get completed and returned assignments
        history_response = supabase.table('ot_assignments').select(
            '*, member:members!ot_assignments_member_id_fkey(id, member_name, phone, branch_id), trainer:users!ot_assignments_trainer_id_fkey(name)'
        ).in_('status', ['completed', 'returned']).order('assigned_at', desc=True).execute()

        history_data = history_response.data or []

        # Filter by branch for branch_admin
        if user['role'] == 'branch_admin':
            history_data = [h for h in history_data if h.get('member', {}).get('branch_id') == user['branch_id']]

        return jsonify({'success': True, 'history': history_data})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Increase OT Sessions
@app.route('/ot-members/<member_id>/increase-sessions', methods=['POST'])
@role_required('main_admin', 'branch_admin')
def increase_ot_sessions(member_id):
    """Increase the number of OT sessions for a member"""
    user = session['user']
    additional_sessions = request.form.get('additional_sessions', '1')

    try:
        additional_sessions = int(additional_sessions)
        if additional_sessions < 1:
            additional_sessions = 1
    except:
        additional_sessions = 1

    try:
        # Get member
        member_response = supabase.table('members').select('*').eq('id', member_id).execute()
        if not member_response.data:
            flash('회원을 찾을 수 없습니다.', 'error')
            return redirect(url_for('ot_members'))

        member = member_response.data[0]

        if member.get('member_type') != 'OT회원':
            flash('OT 회원만 세션을 추가할 수 있습니다.', 'error')
            return redirect(url_for('ot_members'))

        # Update sessions count
        new_sessions = member.get('sessions', 1) + additional_sessions
        new_remaining = (member.get('ot_remaining_sessions') or 0) + additional_sessions

        # Determine new status
        new_status = member.get('ot_status', 'unassigned')
        if new_remaining > 0 and new_status == 'assigned':
            new_status = 'partial'
        elif new_remaining > 0 and new_status == 'completed':
            new_status = 'partial'

        supabase.table('members').update({
            'sessions': new_sessions,
            'ot_remaining_sessions': new_remaining,
            'ot_status': new_status
        }).eq('id', member_id).execute()

        # Record history
        supabase.table('ot_assignment_history').insert({
            'member_id': member_id,
            'action': 'sessions_increased',
            'action_by': user['id'],
            'notes': f'세션 추가: +{additional_sessions}회 (총 {new_sessions}회)'
        }).execute()

        flash(f'{member["member_name"]}님의 세션이 {additional_sessions}회 추가되었습니다. (총 {new_sessions}회)', 'success')

    except Exception as e:
        flash(f'세션 추가 중 오류: {str(e)}', 'error')

    return redirect(url_for('ot_members'))


# Get OT Member Detail with History (AJAX)
@app.route('/ot-members/<member_id>/detail')
@role_required('main_admin', 'branch_admin')
def get_ot_member_detail(member_id):
    """Get detailed info about an OT member including assignment history"""
    try:
        # Get member info
        member_response = supabase.table('members').select('*').eq('id', member_id).execute()
        if not member_response.data:
            return jsonify({'success': False, 'error': '회원을 찾을 수 없습니다.'}), 404

        member = member_response.data[0]

        # Get all assignments
        assignments_response = supabase.table('ot_assignments').select(
            '*, trainer:users!ot_assignments_trainer_id_fkey(id, name)'
        ).eq('member_id', member_id).order('session_number').execute()
        assignments = assignments_response.data or []

        # Get assignment history
        history_response = supabase.table('ot_assignment_history').select(
            '*, trainer:users!ot_assignment_history_trainer_id_fkey(name), action_by_user:users!ot_assignment_history_action_by_fkey(name)'
        ).eq('member_id', member_id).order('action_at', desc=True).execute()
        history = history_response.data or []

        # Check schedule status for each assignment
        for assignment in assignments:
            schedule_response = supabase.table('schedules').select('id, status, schedule_date').eq(
                'member_id', member_id
            ).eq('trainer_id', assignment['trainer_id']).execute()

            assignment['schedule_status'] = 'not_scheduled'
            assignment['schedule_date'] = None
            for sch in (schedule_response.data or []):
                if sch['status'] == '수업 완료':
                    assignment['schedule_status'] = 'completed'
                    assignment['schedule_date'] = sch['schedule_date']
                    break
                elif sch['status'] == '수업 계획':
                    assignment['schedule_status'] = 'scheduled'
                    assignment['schedule_date'] = sch['schedule_date']

        return jsonify({
            'success': True,
            'member': member,
            'assignments': assignments,
            'history': history
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
