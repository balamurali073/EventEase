import os
import functools
from datetime import datetime, timezone
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from database import (
    get_db_connection, init_db, ensure_db_ready, reserve_tickets,
    process_checkout, cancel_reservation, cleanup_expired_reservations,
    start_background_cleanup_worker, utc_now
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static')
)
app.secret_key = os.environ.get('SECRET_KEY', 'eventease-super-secret-production-key-2026')

# Ensure DB tables & initial seed exist (essential for Vercel /tmp/ serverless cold starts)
ensure_db_ready()

# Daemon background thread should only run in persistent servers, not in ephemeral serverless containers
if not os.environ.get('VERCEL') and not os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
    start_background_cleanup_worker(interval_seconds=30)

# -------------------------------------------------------------
# Context Processors & Template Filters
# -------------------------------------------------------------
@app.context_processor
def inject_user_and_now():
    return {
        'current_user': {
            'id': session.get('user_id'),
            'username': session.get('username'),
            'role': session.get('role'),
            'email': session.get('email')
        } if 'user_id' in session else None,
        'current_year': datetime.now().year
    }

@app.template_filter('datetime_format')
def datetime_format(value, format="%b %d, %Y - %I:%M %p"):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace(' ', 'T'))
        return dt.strftime(format)
    except Exception:
        return value

# -------------------------------------------------------------
# Role-Based Authentication Decorators
# -------------------------------------------------------------
def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please sign in to access this page.", "warning")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def role_required(allowed_roles):
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash("Please sign in to continue.", "warning")
                return redirect(url_for('login', next=request.url))
            if session.get('role') not in allowed_roles:
                flash("Access denied: You do not have permission for this resource.", "danger")
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# -------------------------------------------------------------
# Authentication Routes
# -------------------------------------------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        role = request.form.get('role', 'attendee')

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return render_template('register.html', username=username, email=email, role=role)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('register.html', username=username, email=email, role=role)

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template('register.html', username=username, email=email, role=role)

        if role not in ('attendee', 'organizer', 'admin'):
            role = 'attendee'

        conn = get_db_connection()
        try:
            existing = conn.execute(
                "SELECT id FROM users WHERE username = ? OR email = ?",
                (username, email)
            ).fetchone()
            if existing:
                flash("Username or email is already registered.", "danger")
                return render_template('register.html', username=username, email=email, role=role)

            hashed_pw = generate_password_hash(password)
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (username, email, hashed_pw, role)
            )
            conn.commit()
            user_id = cur.lastrowid

            session['user_id'] = user_id
            session['username'] = username
            session['email'] = email
            session['role'] = role

            flash(f"Welcome to EventEase, {username}! Your account has been created.", "success")
            if role == 'organizer':
                return redirect(url_for('organizer_dashboard'))
            elif role == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('index'))

        except Exception as e:
            flash(f"Error during registration: {str(e)}", "danger")
            return render_template('register.html')
        finally:
            conn.close()

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        login_id = request.form.get('login_id', '').strip()
        password = request.form.get('password', '')
        next_url = request.form.get('next') or request.args.get('next')

        if not login_id or not password:
            flash("Please enter your email/username and password.", "danger")
            return render_template('login.html', login_id=login_id)

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE (email = ? OR username = ?)",
            (login_id.lower(), login_id)
        ).fetchone()
        conn.close()

        if not user or not check_password_hash(user['password_hash'], password):
            flash("Invalid credentials. Please verify your email/username and password.", "danger")
            return render_template('login.html', login_id=login_id)

        if not user['is_active']:
            flash("Your account has been deactivated by an administrator.", "danger")
            return render_template('login.html')

        session['user_id'] = user['id']
        session['username'] = user['username']
        session['email'] = user['email']
        session['role'] = user['role']

        flash(f"Welcome back, {user['username']}!", "success")

        if next_url and next_url.startswith('/'):
            return redirect(next_url)

        if user['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        elif user['role'] == 'organizer':
            return redirect(url_for('organizer_dashboard'))
        return redirect(url_for('index'))

    return render_template('login.html', next=request.args.get('next', ''))

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been securely signed out.", "info")
    return redirect(url_for('index'))

# -------------------------------------------------------------
# Public & Attendee Event Discovery
# -------------------------------------------------------------
@app.route('/')
def index():
    cleanup_expired_reservations()
    search_query = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()

    conn = get_db_connection()
    sql = """
        SELECT e.*, u.username as organizer_name,
               MIN(t.price) as min_price,
               MAX(t.price) as max_price,
               SUM(t.available_quantity) as total_available_tickets
        FROM events e
        JOIN users u ON e.organizer_id = u.id
        LEFT JOIN ticket_types t ON e.id = t.event_id
        WHERE e.status = 'PUBLISHED'
    """
    params = []

    if search_query:
        sql += " AND (e.title LIKE ? OR e.description LIKE ? OR e.venue LIKE ?)"
        wildcard = f"%{search_query}%"
        params.extend([wildcard, wildcard, wildcard])

    if category and category != 'All':
        sql += " AND e.category = ?"
        params.append(category)

    sql += " GROUP BY e.id ORDER BY e.event_date ASC"

    events = conn.execute(sql, params).fetchall()

    cats = conn.execute(
        "SELECT DISTINCT category FROM events WHERE status = 'PUBLISHED' ORDER BY category"
    ).fetchall()
    categories = [row['category'] for row in cats]

    conn.close()
    return render_template('index.html', events=events, categories=categories,
                           selected_category=category, search_query=search_query)

@app.route('/events/<int:event_id>')
def event_detail(event_id):
    cleanup_expired_reservations()
    conn = get_db_connection()

    event = conn.execute("""
        SELECT e.*, u.username as organizer_name, u.email as organizer_email
        FROM events e
        JOIN users u ON e.organizer_id = u.id
        WHERE e.id = ?
    """, (event_id,)).fetchone()

    if not event:
        conn.close()
        abort(404)

    ticket_types = conn.execute("""
        SELECT * FROM ticket_types
        WHERE event_id = ?
        ORDER BY price ASC
    """, (event_id,)).fetchall()

    conn.close()

    is_sales_open = True
    try:
        cutoff = datetime.fromisoformat(event['sales_cutoff_date'].replace(' ', 'T'))
        if utc_now() > cutoff:
            is_sales_open = False
    except Exception:
        pass

    return render_template('event_detail.html', event=event,
                           ticket_types=ticket_types, is_sales_open=is_sales_open)

# -------------------------------------------------------------
# Ticket Reservation & Temporary Hold
# -------------------------------------------------------------
@app.route('/reserve', methods=['POST'])
@login_required
def reserve():
    ticket_type_id = request.form.get('ticket_type_id', type=int)
    quantity = request.form.get('quantity', type=int, default=1)

    if not ticket_type_id or quantity <= 0:
        flash("Invalid ticket selection or quantity.", "danger")
        return redirect(request.referrer or url_for('index'))

    success, result, token = reserve_tickets(session['user_id'], ticket_type_id, quantity)

    if not success:
        flash(result, "danger")
        return redirect(request.referrer or url_for('index'))

    flash("Tickets placed on a 10-minute hold! Complete checkout to confirm your booking.", "success")
    return redirect(url_for('checkout', token=token))

@app.route('/checkout/<token>', methods=['GET', 'POST'])
@login_required
def checkout(token):
    cleanup_expired_reservations()
    conn = get_db_connection()

    res = conn.execute("""
        SELECT r.*, t.name as ticket_name, e.id as event_id, e.title as event_title,
               e.venue, e.event_date, e.banner_url
        FROM reservations r
        JOIN ticket_types t ON r.ticket_type_id = t.id
        JOIN events e ON t.event_id = e.id
        WHERE r.reservation_token = ?
    """, (token,)).fetchone()
    conn.close()

    if not res:
        flash("Reservation session not found.", "danger")
        return redirect(url_for('index'))

    if res['user_id'] != session['user_id']:
        flash("Unauthorized reservation access.", "danger")
        return redirect(url_for('index'))

    if res['status'] == 'CONFIRMED':
        flash("This reservation has already been confirmed.", "info")
        conn = get_db_connection()
        booking = conn.execute("SELECT booking_reference FROM bookings WHERE reservation_id = ?", (res['id'],)).fetchone()
        conn.close()
        if booking:
            return redirect(url_for('confirmation', booking_ref=booking['booking_reference']))
        return redirect(url_for('my_bookings'))

    if res['status'] in ('EXPIRED', 'CANCELLED'):
        flash(f"This reservation is {res['status'].lower()}. Tickets were released back to inventory.", "warning")
        return redirect(url_for('event_detail', event_id=res['event_id']))

    expires_at = datetime.fromisoformat(res['expires_at'].replace(' ', 'T'))
    remaining_seconds = max(0, int((expires_at - utc_now()).total_seconds()))

    if remaining_seconds <= 0:
        cleanup_expired_reservations()
        flash("Your 10-minute reservation hold has expired. Please select tickets again.", "warning")
        return redirect(url_for('event_detail', event_id=res['event_id']))

    if request.method == 'POST':
        payment_method = request.form.get('payment_method', 'Credit Card')
        simulation_mode = request.form.get('simulation_mode', 'RANDOM')

        force_outcome = None
        if simulation_mode == 'FORCE_SUCCESS':
            force_outcome = 'SUCCESS'
        elif simulation_mode == 'FORCE_FAILURE':
            force_outcome = 'FAILED'

        success, result = process_checkout(token, session['user_id'], payment_method, force_outcome)

        if success:
            flash("Payment successful! Your tickets are confirmed.", "success")
            return redirect(url_for('confirmation', booking_ref=result['booking_reference']))
        else:
            flash(result, "danger")
            return redirect(url_for('event_detail', event_id=res['event_id']))

    return render_template('checkout.html', reservation=res, remaining_seconds=remaining_seconds)

@app.route('/cancel-reservation/<token>', methods=['POST'])
@login_required
def cancel_reservation_route(token):
    success, msg = cancel_reservation(token, session['user_id'])
    if success:
        flash("Reservation cancelled. Your tickets have been returned to available stock.", "info")
    else:
        flash(msg, "warning")
    return redirect(url_for('index'))

@app.route('/confirmation/<booking_ref>')
@login_required
def confirmation(booking_ref):
    conn = get_db_connection()
    booking = conn.execute("""
        SELECT b.*, e.title as event_title, e.venue, e.event_date, e.banner_url,
               t.name as ticket_name, t.price as unit_price,
               tx.transaction_reference, tx.payment_method, tx.created_at as payment_time,
               u.username, u.email
        FROM bookings b
        JOIN events e ON b.event_id = e.id
        JOIN ticket_types t ON b.ticket_type_id = t.id
        JOIN transactions tx ON b.id = tx.booking_id
        JOIN users u ON b.user_id = u.id
        WHERE b.booking_reference = ?
    """, (booking_ref,)).fetchone()
    conn.close()

    if not booking:
        flash("Booking confirmation not found.", "danger")
        return redirect(url_for('index'))

    if booking['user_id'] != session['user_id'] and session.get('role') != 'admin':
        flash("Unauthorized booking access.", "danger")
        return redirect(url_for('index'))

    return render_template('booking_confirmation.html', booking=booking)

@app.route('/my-bookings')
@login_required
def my_bookings():
    conn = get_db_connection()
    bookings = conn.execute("""
        SELECT b.*, e.title as event_title, e.venue, e.event_date, e.banner_url,
               t.name as ticket_name, tx.transaction_reference, tx.payment_method
        FROM bookings b
        JOIN events e ON b.event_id = e.id
        JOIN ticket_types t ON b.ticket_type_id = t.id
        LEFT JOIN transactions tx ON b.id = tx.booking_id
        WHERE b.user_id = ?
        ORDER BY b.created_at DESC
    """, (session['user_id'],)).fetchall()
    conn.close()

    return render_template('my_bookings.html', bookings=bookings)

# -------------------------------------------------------------
# Organizer Dashboard & Event Management
# -------------------------------------------------------------
@app.route('/organizer/dashboard')
@login_required
@role_required(['organizer', 'admin'])
def organizer_dashboard():
    cleanup_expired_reservations()
    user_id = session['user_id']
    is_admin = session.get('role') == 'admin'

    conn = get_db_connection()

    if is_admin:
        events = conn.execute("""
            SELECT e.*, u.username as organizer_name,
                   COALESCE(SUM(t.total_quantity), 0) as total_tickets,
                   COALESCE(SUM(t.available_quantity), 0) as available_tickets,
                   COALESCE(SUM(t.total_quantity - t.available_quantity), 0) as reserved_or_sold
            FROM events e
            JOIN users u ON e.organizer_id = u.id
            LEFT JOIN ticket_types t ON e.id = t.event_id
            GROUP BY e.id
            ORDER BY e.created_at DESC
        """).fetchall()

        revenue_row = conn.execute("""
            SELECT COALESCE(SUM(total_amount), 0) as total_revenue,
                   COALESCE(SUM(quantity), 0) as total_tickets_sold,
                   COUNT(id) as total_bookings
            FROM bookings
            WHERE status = 'CONFIRMED'
        """).fetchone()

        recent_bookings = conn.execute("""
            SELECT b.*, e.title as event_title, u.username as attendee_username, t.name as ticket_name
            FROM bookings b
            JOIN events e ON b.event_id = e.id
            JOIN users u ON b.user_id = u.id
            JOIN ticket_types t ON b.ticket_type_id = t.id
            ORDER BY b.created_at DESC
            LIMIT 10
        """).fetchall()
    else:
        events = conn.execute("""
            SELECT e.*,
                   COALESCE(SUM(t.total_quantity), 0) as total_tickets,
                   COALESCE(SUM(t.available_quantity), 0) as available_tickets,
                   COALESCE(SUM(t.total_quantity - t.available_quantity), 0) as reserved_or_sold
            FROM events e
            LEFT JOIN ticket_types t ON e.id = t.event_id
            WHERE e.organizer_id = ?
            GROUP BY e.id
            ORDER BY e.created_at DESC
        """, (user_id,)).fetchall()

        revenue_row = conn.execute("""
            SELECT COALESCE(SUM(b.total_amount), 0) as total_revenue,
                   COALESCE(SUM(b.quantity), 0) as total_tickets_sold,
                   COUNT(b.id) as total_bookings
            FROM bookings b
            JOIN events e ON b.event_id = e.id
            WHERE e.organizer_id = ? AND b.status = 'CONFIRMED'
        """, (user_id,)).fetchone()

        recent_bookings = conn.execute("""
            SELECT b.*, e.title as event_title, u.username as attendee_username, t.name as ticket_name
            FROM bookings b
            JOIN events e ON b.event_id = e.id
            JOIN users u ON b.user_id = u.id
            JOIN ticket_types t ON b.ticket_type_id = t.id
            WHERE e.organizer_id = ?
            ORDER BY b.created_at DESC
            LIMIT 10
        """, (user_id,)).fetchall()

    conn.close()

    total_events = len(events)
    total_revenue = revenue_row['total_revenue'] if revenue_row else 0.0
    total_tickets_sold = revenue_row['total_tickets_sold'] if revenue_row else 0
    total_bookings = revenue_row['total_bookings'] if revenue_row else 0

    return render_template('organizer_dashboard.html',
                           events=events,
                           total_events=total_events,
                           total_revenue=total_revenue,
                           total_tickets_sold=total_tickets_sold,
                           total_bookings=total_bookings,
                           recent_bookings=recent_bookings)

@app.route('/organizer/events/new', methods=['GET', 'POST'])
@login_required
@role_required(['organizer', 'admin'])
def create_event():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category = request.form.get('category', 'Technology')
        venue = request.form.get('venue', '').strip()
        event_date = request.form.get('event_date', '').strip()
        sales_cutoff = request.form.get('sales_cutoff_date', '').strip()
        banner_url = request.form.get('banner_url', '').strip()

        ticket_names = request.form.getlist('ticket_name[]')
        ticket_prices = request.form.getlist('ticket_price[]')
        ticket_quantities = request.form.getlist('ticket_quantity[]')

        if not title or not description or not venue or not event_date:
            flash("Please fill in all required event details.", "danger")
            return render_template('create_event.html')

        if not ticket_names:
            flash("At least one ticket category (e.g., General Admission, VIP) is required.", "danger")
            return render_template('create_event.html')

        if not sales_cutoff:
            sales_cutoff = event_date

        if not banner_url:
            banner_url = "https://images.unsplash.com/photo-1540575467063-178a50c2df87?auto=format&fit=crop&w=1200&q=80"

        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("""
                INSERT INTO events (
                    organizer_id, title, description, category, venue,
                    event_date, sales_cutoff_date, banner_url, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED')
            """, (session['user_id'], title, description, category, venue,
                  event_date, sales_cutoff, banner_url))

            event_id = cur.lastrowid

            for name, price_str, qty_str in zip(ticket_names, ticket_prices, ticket_quantities):
                name = name.strip()
                if not name:
                    continue
                price = float(price_str) if price_str else 0.0
                qty = int(qty_str) if qty_str else 100
                conn.execute("""
                    INSERT INTO ticket_types (event_id, name, price, total_quantity, available_quantity)
                    VALUES (?, ?, ?, ?, ?)
                """, (event_id, name, price, qty, qty))

            conn.commit()
            flash("Event and ticket categories successfully published!", "success")
            return redirect(url_for('organizer_dashboard'))
        except Exception as e:
            conn.rollback()
            flash(f"Failed to create event: {str(e)}", "danger")
            return render_template('create_event.html')
        finally:
            conn.close()

    return render_template('create_event.html')

@app.route('/organizer/events/<int:event_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required(['organizer', 'admin'])
def edit_event(event_id):
    conn = get_db_connection()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    if not event:
        conn.close()
        abort(404)

    if event['organizer_id'] != session['user_id'] and session.get('role') != 'admin':
        conn.close()
        flash("You do not have permission to edit this event.", "danger")
        return redirect(url_for('organizer_dashboard'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category = request.form.get('category', event['category'])
        venue = request.form.get('venue', '').strip()
        event_date = request.form.get('event_date', '').strip()
        sales_cutoff = request.form.get('sales_cutoff_date', '').strip()
        banner_url = request.form.get('banner_url', '').strip()
        status = request.form.get('status', event['status'])

        try:
            conn.execute("""
                UPDATE events
                SET title = ?, description = ?, category = ?, venue = ?,
                    event_date = ?, sales_cutoff_date = ?, banner_url = ?, status = ?
                WHERE id = ?
            """, (title, description, category, venue, event_date, sales_cutoff, banner_url, status, event_id))

            new_names = request.form.getlist('new_ticket_name[]')
            new_prices = request.form.getlist('new_ticket_price[]')
            new_quantities = request.form.getlist('new_ticket_quantity[]')

            for name, price_str, qty_str in zip(new_names, new_prices, new_quantities):
                name = name.strip()
                if name:
                    price = float(price_str) if price_str else 0.0
                    qty = int(qty_str) if qty_str else 50
                    conn.execute("""
                        INSERT INTO ticket_types (event_id, name, price, total_quantity, available_quantity)
                        VALUES (?, ?, ?, ?, ?)
                    """, (event_id, name, price, qty, qty))

            conn.commit()
            flash("Event details updated successfully.", "success")
            return redirect(url_for('organizer_dashboard'))
        except Exception as e:
            conn.rollback()
            flash(f"Failed to update event: {str(e)}", "danger")

    ticket_types = conn.execute("SELECT * FROM ticket_types WHERE event_id = ?", (event_id,)).fetchall()
    conn.close()
    return render_template('edit_event.html', event=event, ticket_types=ticket_types)

@app.route('/organizer/events/<int:event_id>/attendees')
@login_required
@role_required(['organizer', 'admin'])
def event_attendees(event_id):
    conn = get_db_connection()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        conn.close()
        abort(404)

    if event['organizer_id'] != session['user_id'] and session.get('role') != 'admin':
        conn.close()
        flash("Unauthorized to view attendee list.", "danger")
        return redirect(url_for('organizer_dashboard'))

    attendees = conn.execute("""
        SELECT b.*, u.username, u.email, t.name as ticket_name, tx.transaction_reference, tx.payment_method
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        JOIN ticket_types t ON b.ticket_type_id = t.id
        LEFT JOIN transactions tx ON b.id = tx.booking_id
        WHERE b.event_id = ?
        ORDER BY b.created_at DESC
    """, (event_id,)).fetchall()
    conn.close()

    return render_template('event_attendees.html', event=event, attendees=attendees)

# -------------------------------------------------------------
# Admin Dashboard & Governance
# -------------------------------------------------------------
@app.route('/admin/dashboard')
@login_required
@role_required(['admin'])
def admin_dashboard():
    cleanup_expired_reservations()
    conn = get_db_connection()

    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_organizers = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'organizer'").fetchone()[0]
    total_attendees = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'attendee'").fetchone()[0]
    total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    total_bookings = conn.execute("SELECT COUNT(*) FROM bookings WHERE status = 'CONFIRMED'").fetchone()[0]
    total_revenue = conn.execute("SELECT COALESCE(SUM(total_amount), 0) FROM bookings WHERE status = 'CONFIRMED'").fetchone()[0]

    users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    events = conn.execute("""
        SELECT e.*, u.username as organizer_name,
               COALESCE(SUM(t.total_quantity), 0) as total_tickets,
               COALESCE(SUM(t.available_quantity), 0) as available_tickets
        FROM events e
        JOIN users u ON e.organizer_id = u.id
        LEFT JOIN ticket_types t ON e.id = t.event_id
        GROUP BY e.id
        ORDER BY e.created_at DESC
    """).fetchall()

    transactions = conn.execute("""
        SELECT tx.*, u.username, u.email
        FROM transactions tx
        JOIN users u ON tx.user_id = u.id
        ORDER BY tx.created_at DESC
        LIMIT 25
    """).fetchall()

    conn.close()

    metrics = {
        'total_users': total_users,
        'total_organizers': total_organizers,
        'total_attendees': total_attendees,
        'total_events': total_events,
        'total_bookings': total_bookings,
        'total_revenue': total_revenue
    }

    return render_template('admin_dashboard.html', metrics=metrics,
                           users=users, events=events, transactions=transactions)

@app.route('/admin/users/<int:user_id>/toggle-status', methods=['POST'])
@login_required
@role_required(['admin'])
def toggle_user_status(user_id):
    if user_id == session['user_id']:
        flash("You cannot deactivate your own admin account.", "warning")
        return redirect(url_for('admin_dashboard'))

    conn = get_db_connection()
    user = conn.execute("SELECT is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        new_status = 0 if user['is_active'] else 1
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, user_id))
        conn.commit()
        status_text = "activated" if new_status else "deactivated"
        flash(f"User account has been {status_text}.", "success")
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/events/<int:event_id>/toggle-status', methods=['POST'])
@login_required
@role_required(['admin'])
def toggle_event_status(event_id):
    conn = get_db_connection()
    event = conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()
    if event:
        new_status = 'CANCELLED' if event['status'] == 'PUBLISHED' else 'PUBLISHED'
        conn.execute("UPDATE events SET status = ? WHERE id = ?", (new_status, event_id))
        conn.commit()
        flash(f"Event status changed to {new_status}.", "success")
    conn.close()
    return redirect(url_for('admin_dashboard'))

# -------------------------------------------------------------
# REST API Endpoints (For Postman, AJAX, and Chart.js)
# -------------------------------------------------------------
@app.route('/api/reservation-status/<token>')
def api_reservation_status(token):
    cleanup_expired_reservations()
    conn = get_db_connection()
    res = conn.execute("SELECT status, expires_at FROM reservations WHERE reservation_token = ?", (token,)).fetchone()
    conn.close()

    if not res:
        return jsonify({'valid': False, 'message': 'Reservation not found'}), 404

    expires_at = datetime.fromisoformat(res['expires_at'].replace(' ', 'T'))
    remaining = max(0, int((expires_at - utc_now()).total_seconds()))

    return jsonify({
        'valid': res['status'] == 'PENDING' and remaining > 0,
        'status': res['status'],
        'remaining_seconds': remaining
    })

@app.route('/api/organizer/chart-data')
@login_required
@role_required(['organizer', 'admin'])
def api_organizer_chart_data():
    user_id = session['user_id']
    is_admin = session.get('role') == 'admin'

    conn = get_db_connection()
    if is_admin:
        data = conn.execute("""
            SELECT e.title,
                   COALESCE(SUM(b.total_amount), 0) as revenue,
                   COALESCE(SUM(b.quantity), 0) as tickets_sold
            FROM events e
            LEFT JOIN bookings b ON e.id = b.event_id AND b.status = 'CONFIRMED'
            GROUP BY e.id
            ORDER BY revenue DESC
            LIMIT 7
        """).fetchall()
    else:
        data = conn.execute("""
            SELECT e.title,
                   COALESCE(SUM(b.total_amount), 0) as revenue,
                   COALESCE(SUM(b.quantity), 0) as tickets_sold
            FROM events e
            LEFT JOIN bookings b ON e.id = b.event_id AND b.status = 'CONFIRMED'
            WHERE e.organizer_id = ?
            GROUP BY e.id
            ORDER BY revenue DESC
            LIMIT 7
        """, (user_id,)).fetchall()
    conn.close()

    labels = [row['title'][:25] + ('...' if len(row['title']) > 25 else '') for row in data]
    revenues = [row['revenue'] for row in data]
    tickets_sold = [row['tickets_sold'] for row in data]

    return jsonify({
        'labels': labels,
        'revenues': revenues,
        'tickets_sold': tickets_sold
    })

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    role = data.get('role', 'attendee')

    if not username or not email or not password:
        return jsonify({'error': 'username, email, and password required'}), 400

    conn = get_db_connection()
    existing = conn.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'Username or email already exists'}), 409

    hashed_pw = generate_password_hash(password)
    cur = conn.execute(
        "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)",
        (username, email, hashed_pw, role)
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()

    return jsonify({'message': 'User registered successfully', 'user_id': user_id, 'username': username, 'role': role}), 201

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    login_id = data.get('login_id', '').strip()
    password = data.get('password', '')

    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE (email = ? OR username = ?)", (login_id.lower(), login_id)).fetchone()
    conn.close()

    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid credentials'}), 401

    if not user['is_active']:
        return jsonify({'error': 'User account deactivated'}), 403

    session['user_id'] = user['id']
    session['username'] = user['username']
    session['email'] = user['email']
    session['role'] = user['role']

    return jsonify({
        'message': 'Login successful',
        'user': {
            'id': user['id'],
            'username': user['username'],
            'email': user['email'],
            'role': user['role']
        }
    }), 200

@app.route('/api/events', methods=['GET'])
def api_events():
    category = request.args.get('category')
    search = request.args.get('q')

    conn = get_db_connection()
    sql = """
        SELECT e.*, u.username as organizer_name,
               MIN(t.price) as min_price,
               SUM(t.available_quantity) as total_available_tickets
        FROM events e
        JOIN users u ON e.organizer_id = u.id
        LEFT JOIN ticket_types t ON e.id = t.event_id
        WHERE e.status = 'PUBLISHED'
    """
    params = []
    if search:
        sql += " AND (e.title LIKE ? OR e.description LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if category:
        sql += " AND e.category = ?"
        params.append(category)

    sql += " GROUP BY e.id ORDER BY e.event_date ASC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    events = [dict(row) for row in rows]
    return jsonify({'count': len(events), 'events': events})

@app.route('/api/events/<int:event_id>', methods=['GET'])
def api_event_detail(event_id):
    conn = get_db_connection()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        conn.close()
        return jsonify({'error': 'Event not found'}), 404

    tickets = conn.execute("SELECT * FROM ticket_types WHERE event_id = ?", (event_id,)).fetchall()
    conn.close()

    res = dict(event)
    res['ticket_types'] = [dict(t) for t in tickets]
    return jsonify(res)

@app.route('/api/reservations', methods=['POST'])
def api_create_reservation():
    data = request.get_json() or {}
    user_id = session.get('user_id') or data.get('user_id')
    ticket_type_id = data.get('ticket_type_id')
    quantity = data.get('quantity', 1)

    if not user_id:
        return jsonify({'error': 'Authentication required (session or user_id)'}), 401
    if not ticket_type_id or quantity <= 0:
        return jsonify({'error': 'Valid ticket_type_id and quantity > 0 required'}), 400

    success, result, token = reserve_tickets(user_id, ticket_type_id, quantity)
    if not success:
        return jsonify({'error': result}), 400

    return jsonify({
        'message': 'Reservation created with 10-minute hold',
        'reservation': result
    }), 201

@app.route('/api/checkout', methods=['POST'])
def api_checkout():
    data = request.get_json() or {}
    token = data.get('reservation_token')
    user_id = session.get('user_id') or data.get('user_id')
    payment_method = data.get('payment_method', 'Credit Card')
    force_outcome = data.get('force_outcome')

    if not token or not user_id:
        return jsonify({'error': 'reservation_token and user_id required'}), 400

    success, result = process_checkout(token, user_id, payment_method, force_outcome)
    if not success:
        return jsonify({'error': result}), 400

    return jsonify({
        'message': 'Payment successful and booking confirmed',
        'booking': result
    }), 200

@app.route('/api/bookings', methods=['GET'])
def api_bookings():
    user_id = session.get('user_id') or request.args.get('user_id', type=int)
    if not user_id:
        return jsonify({'error': 'user_id required'}), 401

    conn = get_db_connection()
    rows = conn.execute("""
        SELECT b.*, e.title as event_title, t.name as ticket_name, tx.transaction_reference
        FROM bookings b
        JOIN events e ON b.event_id = e.id
        JOIN ticket_types t ON b.ticket_type_id = t.id
        LEFT JOIN transactions tx ON b.id = tx.booking_id
        WHERE b.user_id = ?
        ORDER BY b.created_at DESC
    """, (user_id,)).fetchall()
    conn.close()

    return jsonify({'count': len(rows), 'bookings': [dict(r) for r in rows]})

@app.route('/api/admin/metrics', methods=['GET'])
def api_admin_metrics():
    conn = get_db_connection()
    metrics = {
        'total_users': conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'total_organizers': conn.execute("SELECT COUNT(*) FROM users WHERE role = 'organizer'").fetchone()[0],
        'total_events': conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        'total_bookings': conn.execute("SELECT COUNT(*) FROM bookings WHERE status = 'CONFIRMED'").fetchone()[0],
        'total_revenue': conn.execute("SELECT COALESCE(SUM(total_amount), 0) FROM bookings WHERE status = 'CONFIRMED'").fetchone()[0]
    }
    conn.close()
    return jsonify(metrics)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
