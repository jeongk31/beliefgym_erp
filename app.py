from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from supabase import create_client, Client
from functools import wraps
from datetime import datetime, timedelta, timezone
import config

# Korean timezone (UTC+9)
KST = timezone(timedelta(hours=9))

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

        # Sales this month (excluding refunded, 50% for WI)
        dashboard_data['sales_this_month'] = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in new_this_month if m.get('refund_status') != 'refunded'
        )

        # Sales last month
        dashboard_data['sales_last_month'] = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in new_last_month if m.get('refund_status') != 'refunded'
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

            # Sales this month
            dashboard_data['sales_this_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_this_month if m.get('refund_status') != 'refunded'
            )
            dashboard_data['sales_last_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_last_month if m.get('refund_status') != 'refunded'
            )

            # Sessions this month
            sessions_month = supabase.table('schedules').select('id').in_('trainer_id', trainer_ids).eq('status', '수업 완료').gte('schedule_date', month_start.isoformat()).lt('schedule_date', next_month.isoformat()).execute()
            dashboard_data['sessions_this_month'] = len(sessions_month.data or [])

            # Top trainers by sales this month
            trainer_sales = {}
            for m in new_this_month:
                if m.get('refund_status') != 'refunded':
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

            # Sales this month
            dashboard_data['sales_this_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_this_month if m.get('refund_status') != 'refunded'
            )
            dashboard_data['sales_last_month'] = sum(
                m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
                for m in new_last_month if m.get('refund_status') != 'refunded'
            )

            # Sessions this month
            sessions_month = supabase.table('schedules').select('id').eq('status', '수업 완료').gte('schedule_date', month_start.isoformat()).lt('schedule_date', next_month.isoformat()).execute()
            dashboard_data['sessions_this_month'] = len(sessions_month.data or [])

            # Top trainers by sales
            trainer_sales = {}
            for m in new_this_month:
                if m.get('refund_status') != 'refunded':
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
            response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('trainer_id', filter_trainer_id).order('created_at', desc=True).execute()
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
            response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('trainer_id', filter_trainer_id).order('created_at', desc=True).execute()
            trainer_response = supabase.table('users').select('name').eq('id', filter_trainer_id).execute()
            if trainer_response.data:
                filter_trainer_name = trainer_response.data[0]['name']
        else:
            # No trainer selected - show empty until selection
            response = type('obj', (object,), {'data': []})()

    else:  # trainer
        response = supabase.table('members').select('*, trainer:users!members_trainer_id_fkey(name)').eq('trainer_id', user['id']).order('created_at', desc=True).execute()

    members_list = response.data if response.data else []

    # Get all member IDs
    member_ids = [m['id'] for m in members_list]

    # Fetch all schedules for these members in the selected month
    if member_ids:
        schedules_response = supabase.table('schedules').select('*').in_('member_id', member_ids).gte('schedule_date', month_start.isoformat()).lte('schedule_date', month_end.isoformat()).execute()
        schedules = schedules_response.data if schedules_response.data else []

        # Also get total completed sessions for each member (all time)
        all_schedules_response = supabase.table('schedules').select('member_id, status').in_('member_id', member_ids).eq('status', '수업 완료').execute()
        all_completed = all_schedules_response.data if all_schedules_response.data else []
    else:
        schedules = []
        all_completed = []

    # Count completed sessions per member
    completed_counts = {}
    for s in all_completed:
        mid = s['member_id']
        completed_counts[mid] = completed_counts.get(mid, 0) + 1

    # Organize schedules by member and date (list of schedules per date)
    schedule_map = {}  # {member_id: {date: [schedules]}}
    for s in schedules:
        mid = s['member_id']
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

        # Determine trainer_id
        if user['role'] == 'trainer':
            trainer_id = user['id']
        else:
            trainer_id = request.form.get('trainer_id')

        # Validate required fields
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
                'trainer_id': trainer_id,
                'created_by': user['id']
            }

            supabase.table('members').insert(member_data).execute()
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
    selected_trainer_id = request.args.get('trainer_id')

    if user['role'] == 'main_admin':
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
        '*, member:members!schedules_member_id_fkey(member_name), trainer:users!schedules_trainer_id_fkey(name)'
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

    # Get members for quick-add feature
    if user['role'] == 'trainer':
        members_response = supabase.table('members').select('id, member_name').eq('trainer_id', user['id']).execute()
    elif user['role'] == 'main_admin':
        members_response = supabase.table('members').select('id, member_name, trainer_id').execute()
    else:  # branch_admin
        branch_trainers = supabase.table('users').select('id').eq('branch_id', user['branch_id']).execute()
        trainer_ids = [t['id'] for t in branch_trainers.data] if branch_trainers.data else []
        members_response = supabase.table('members').select('id, member_name, trainer_id').in_('trainer_id', trainer_ids).execute()

    members_list = members_response.data if members_response.data else []

    return render_template('schedule.html',
                         user=user,
                         schedule_grid=schedule_grid,
                         week_days=week_days,
                         time_slots=time_slots,
                         selected_date=selected_date.isoformat(),
                         week_start=week_start.isoformat(),
                         trainers=trainers_list,
                         selected_trainer_id=selected_trainer_id,
                         members=members_list)


@app.route('/schedule/add', methods=['GET', 'POST'])
@login_required
def add_schedule():
    user = session['user']

    # Get members for this trainer
    if user['role'] == 'trainer':
        members_response = supabase.table('members').select('id, member_name').eq('trainer_id', user['id']).execute()
    elif user['role'] in ['main_admin', 'branch_admin']:
        if user['role'] == 'main_admin':
            members_response = supabase.table('members').select('id, member_name, trainer_id').execute()
        else:
            trainers = supabase.table('users').select('id').eq('branch_id', user['branch_id']).execute()
            trainer_ids = [t['id'] for t in trainers.data] if trainers.data else []
            members_response = supabase.table('members').select('id, member_name, trainer_id').in_('trainer_id', trainer_ids).execute()

    members_list = members_response.data if members_response.data else []

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


@app.route('/schedule/quick-add', methods=['POST'])
@login_required
def quick_add_schedule():
    """AJAX endpoint for quickly adding schedules by clicking on time slots"""
    user = session['user']

    data = request.get_json()
    member_id = data.get('member_id')
    schedule_date = data.get('date')
    start_time = data.get('time')

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
        # Trainer can only add their own members
        if member['trainer_id'] != user['id']:
            return jsonify({'success': False, 'error': '본인의 회원만 스케줄에 추가할 수 있습니다.'}), 403
        trainer_id = user['id']
    else:
        # Admin uses the member's assigned trainer
        trainer_id = member['trainer_id']

    try:
        schedule_data = {
            'trainer_id': trainer_id,
            'member_id': member_id,
            'schedule_date': schedule_date,
            'start_time': start_time,
            'end_time': end_time,
            'status': '수업 계획'
        }

        result = supabase.table('schedules').insert(schedule_data).execute()

        if result.data:
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
INCENTIVE_TIERS = [
    (20000000, 5400000),
    (15000000, 4050000),
    (12000000, 3040000),
    (10000000, 2400000),
    (8500000, 1955000),
    (6500000, 1430000),
    (4500000, 1050000),
    (3000000, 480000),
]


def calculate_incentive(sales_amount):
    """Calculate incentive based on sales tier"""
    for tier_threshold, incentive in INCENTIVE_TIERS:
        if sales_amount >= tier_threshold:
            return incentive
    return 0


def calculate_sales_support_incentive(sales_amount):
    """Calculate 영업지원인센티브 based on sales"""
    if sales_amount > 3000000:
        return 1000000
    elif sales_amount > 0:
        return 500000
    return 0


def calculate_master_trainer_bonus(six_month_sales):
    """Calculate Master Trainer 진급 bonus based on 6-month total sales"""
    if six_month_sales >= 9000000:
        return 300000
    return 0


# Lesson fee (수업료) percentage tiers based on 매출
LESSON_FEE_TIERS = [
    (20000000, 35),
    (15000000, 35),
    (12000000, 35),
    (10000000, 34),
    (8500000, 33),
    (6500000, 32),
    (4500000, 31),
    (3000000, 30),
]


def calculate_lesson_fee_rate(sales_amount):
    """Calculate lesson fee percentage based on sales tier"""
    for tier_threshold, rate in LESSON_FEE_TIERS:
        if sales_amount >= tier_threshold:
            return rate
    return 10  # Default 10% for under 3M


def calculate_lesson_fee_rate_other(sales_amount):
    """Calculate 근무외 lesson fee percentage based on sales"""
    if sales_amount > 5000000:
        return 40
    return calculate_lesson_fee_rate(sales_amount)


def calculate_member_sales_contribution(member):
    """Calculate how much a member contributes to sales (considering WI 50% rule)"""
    contract_amount = member['sessions'] * member['unit_price']
    if member.get('channel') == 'WI':
        contract_amount = contract_amount * 0.5
    return contract_amount


def calculate_trainer_incentives_for_month(trainer_id, month_start, next_month, exclude_member_id=None):
    """
    Calculate trainer's incentives (인센티브 + 영업지원인센티브 + Master Trainer bonus) for a specific month.
    Optionally exclude a specific member from the calculation.
    Returns tuple: (total_incentives, sales_amount)
    """
    # Get members created in the month
    members_response = supabase.table('members').select(
        'id, sessions, unit_price, channel'
    ).eq('trainer_id', trainer_id).gte(
        'created_at', month_start.isoformat()
    ).lt('created_at', next_month.isoformat()).execute()

    members_list = members_response.data if members_response.data else []

    # Calculate sales, optionally excluding a member
    sales = 0
    for m in members_list:
        if exclude_member_id and m['id'] == exclude_member_id:
            continue
        # Skip already refunded members
        if m.get('refund_status') == 'refunded':
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
        if m.get('refund_status') == 'refunded':
            continue
        six_month_sales += calculate_member_sales_contribution(m)

    # Calculate incentives
    incentive = calculate_incentive(sales)
    sales_support = calculate_sales_support_incentive(sales)
    master_bonus = calculate_master_trainer_bonus(six_month_sales)

    total_incentives = incentive + sales_support + master_bonus
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
    """Process a member refund"""
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

    # Calculate the deduction amount
    deduction_amount, original_month = calculate_refund_deduction(member_id)

    # Determine which month to apply the deduction
    current_month = datetime.now(KST).date().replace(day=1)

    # Check if member was created in current month
    created_at = datetime.fromisoformat(member['created_at'].replace('Z', '+00:00'))
    member_month = created_at.replace(day=1).date()

    is_same_month = (member_month.year == current_month.year and
                     member_month.month == current_month.month)

    try:
        # Update member with refund info
        update_data = {
            'refund_status': 'refunded',
            'refund_amount': int(deduction_amount) if not is_same_month else 0,
            'refund_original_month': original_month.isoformat() if original_month else None,
            'refund_applied_month': current_month.isoformat(),
            'refunded_at': datetime.now(KST).isoformat(),
            'refunded_by': user['id']
        }

        supabase.table('members').update(update_data).eq('id', member_id).execute()

        if is_same_month:
            flash(f'회원 환불 처리가 완료되었습니다. (동월 등록 - 계약금액 제외)', 'success')
        else:
            flash(f'회원 환불 처리가 완료되었습니다. (차감액: {int(deduction_amount):,}원)', 'success')

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
        # Clear refund info
        update_data = {
            'refund_status': None,
            'refund_amount': None,
            'refund_original_month': None,
            'refund_applied_month': None,
            'refunded_at': None,
            'refunded_by': None
        }

        supabase.table('members').update(update_data).eq('id', member_id).execute()
        flash('환불이 취소되었습니다.', 'success')

    except Exception as e:
        flash(f'환불 취소 중 오류가 발생했습니다: {str(e)}', 'error')

    return redirect(url_for('view_member', member_id=member_id))


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

        # Calculate sales with 50% for WI channel (exclude refunded members)
        sales = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in members_list
            if m.get('refund_status') != 'refunded'
        )
        six_month_sales = sum(
            m['sessions'] * m['unit_price'] * (0.5 if m.get('channel') == 'WI' else 1)
            for m in six_month_members
            if m.get('refund_status') != 'refunded'
        )
        incentive = calculate_incentive(sales)
        sales_support = calculate_sales_support_incentive(sales)
        master_bonus = calculate_master_trainer_bonus(six_month_sales)

        # Calculate lesson fees
        lesson_fee_base_main = 0
        lesson_fee_base_other = 0
        for schedule in schedules_list:
            member_unit_price = all_members.get(schedule['member_id'], 0)
            if schedule['work_type'] == '근무내':
                lesson_fee_base_main += member_unit_price
            else:
                lesson_fee_base_other += member_unit_price

        lesson_fee_rate_main = calculate_lesson_fee_rate(sales)
        lesson_fee_rate_other = calculate_lesson_fee_rate_other(sales)
        lesson_fee_main = int(lesson_fee_base_main * lesson_fee_rate_main / 100)
        lesson_fee_other = int(lesson_fee_base_other * lesson_fee_rate_other / 100)

        total_salary = incentive + sales_support + master_bonus + lesson_fee_main + lesson_fee_other - int(refund_deductions)

        trainer_data.append({
            'id': user['id'],
            'name': user['name'],
            'branch': '-',
            'sales': sales,
            'six_month_sales': six_month_sales,
            'incentive': incentive,
            'sales_support': sales_support,
            'master_bonus': master_bonus,
            'lesson_fee_base_main': lesson_fee_base_main,
            'lesson_fee_base_other': lesson_fee_base_other,
            'lesson_fee_rate_main': lesson_fee_rate_main,
            'lesson_fee_rate_other': lesson_fee_rate_other,
            'lesson_fee_main': lesson_fee_main,
            'lesson_fee_other': lesson_fee_other,
            'refund_deduction': int(refund_deductions),
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

        # Calculate sales (매출) per trainer - current month (50% for WI channel, exclude refunded)
        trainer_sales = {}
        for member in members_list:
            # Skip refunded members
            if member.get('refund_status') == 'refunded':
                continue
            tid = member['trainer_id']
            contract_amount = member['sessions'] * member['unit_price']
            # Apply 50% if channel is WI
            if member.get('channel') == 'WI':
                contract_amount = contract_amount * 0.5
            trainer_sales[tid] = trainer_sales.get(tid, 0) + contract_amount

        # Calculate 6-month sales per trainer (50% for WI channel, exclude refunded)
        trainer_six_month_sales = {}
        for member in six_month_members:
            # Skip refunded members
            if member.get('refund_status') == 'refunded':
                continue
            tid = member['trainer_id']
            contract_amount = member['sessions'] * member['unit_price']
            # Apply 50% if channel is WI
            if member.get('channel') == 'WI':
                contract_amount = contract_amount * 0.5
            trainer_six_month_sales[tid] = trainer_six_month_sales.get(tid, 0) + contract_amount

        # Calculate lesson fee base per trainer
        trainer_lesson_fees = {}
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

        # Build trainer data with sales and incentive
        for trainer in trainers_list:
            sales = trainer_sales.get(trainer['id'], 0)
            six_month_sales = trainer_six_month_sales.get(trainer['id'], 0)
            incentive = calculate_incentive(sales)
            sales_support = calculate_sales_support_incentive(sales)
            master_bonus = calculate_master_trainer_bonus(six_month_sales)

            # Lesson fees
            lesson_data = trainer_lesson_fees.get(trainer['id'], {'main': 0, 'other': 0})
            lesson_fee_base_main = lesson_data['main']
            lesson_fee_base_other = lesson_data['other']
            lesson_fee_rate_main = calculate_lesson_fee_rate(sales)
            lesson_fee_rate_other = calculate_lesson_fee_rate_other(sales)
            lesson_fee_main = int(lesson_fee_base_main * lesson_fee_rate_main / 100)
            lesson_fee_other = int(lesson_fee_base_other * lesson_fee_rate_other / 100)

            # Refund deductions
            refund_deduction = int(trainer_refund_deductions.get(trainer['id'], 0))

            trainer_total = incentive + sales_support + master_bonus + lesson_fee_main + lesson_fee_other - refund_deduction
            total_sales += sales
            total_incentive += trainer_total

            trainer_data.append({
                'id': trainer['id'],
                'name': trainer['name'],
                'branch': trainer['branch']['name'] if trainer.get('branch') else '-',
                'sales': sales,
                'six_month_sales': six_month_sales,
                'incentive': incentive,
                'sales_support': sales_support,
                'master_bonus': master_bonus,
                'lesson_fee_base_main': lesson_fee_base_main,
                'lesson_fee_base_other': lesson_fee_base_other,
                'lesson_fee_rate_main': lesson_fee_rate_main,
                'lesson_fee_rate_other': lesson_fee_rate_other,
                'lesson_fee_main': lesson_fee_main,
                'lesson_fee_other': lesson_fee_other,
                'refund_deduction': refund_deduction,
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
                         incentive_tiers=INCENTIVE_TIERS)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
