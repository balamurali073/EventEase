import sqlite3
import os
import uuid
from datetime import datetime, timezone, timedelta
import threading
import time

IS_VERCEL = bool(os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))

if IS_VERCEL:
    DATABASE_PATH = '/tmp/eventease.db'
else:
    DATABASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'eventease.db')

def utc_now():
    """Returns current UTC datetime without tzinfo for consistent SQLite storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def get_db_connection():
    """Returns a SQLite connection configured with Row factory, WAL mode, foreign keys, and timeout."""
    conn = sqlite3.connect(DATABASE_PATH, timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    # In Vercel serverless /tmp, standard journal or WAL works
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except Exception:
        pass
    conn.execute("PRAGMA busy_timeout = 10000;")
    return conn

def init_db():
    """Initializes all database tables, constraints, and indexes."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Users table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'organizer', 'attendee')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 2. Events table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organizer_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            venue TEXT NOT NULL,
            event_date TEXT NOT NULL,
            sales_cutoff_date TEXT NOT NULL,
            banner_url TEXT,
            status TEXT NOT NULL DEFAULT 'PUBLISHED' CHECK(status IN ('DRAFT', 'PUBLISHED', 'CANCELLED', 'COMPLETED')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (organizer_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)

        # 3. Ticket Types table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticket_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL CHECK(price >= 0),
            total_quantity INTEGER NOT NULL CHECK(total_quantity >= 0),
            available_quantity INTEGER NOT NULL CHECK(available_quantity >= 0),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        );
        """)

        # 4. Reservations table (Temporary 10-minute hold)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_token TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            ticket_type_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price REAL NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'CONFIRMED', 'EXPIRED', 'CANCELLED')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (ticket_type_id) REFERENCES ticket_types(id)
        );
        """)

        # 5. Bookings table (Confirmed ticket orders)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_reference TEXT UNIQUE NOT NULL,
            reservation_id INTEGER UNIQUE,
            user_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            ticket_type_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'CONFIRMED' CHECK(status IN ('CONFIRMED', 'CANCELLED', 'REFUNDED')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (reservation_id) REFERENCES reservations(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (event_id) REFERENCES events(id),
            FOREIGN KEY (ticket_type_id) REFERENCES ticket_types(id)
        );
        """)

        # 6. Transactions table (Mock payment audit logs)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_reference TEXT UNIQUE NOT NULL,
            booking_id INTEGER,
            reservation_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            payment_method TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILED')),
            failure_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (booking_id) REFERENCES bookings(id),
            FOREIGN KEY (reservation_id) REFERENCES reservations(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """)

        # Indexes for fast lookups & high performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_event ON ticket_types(event_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_status_exp ON reservations(status, expires_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_token ON reservations(reservation_token);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_ref ON bookings(booking_reference);")

        conn.commit()

def ensure_db_ready():
    """Ensures database tables and initial demo seed data exist (crucial for Vercel /tmp/ cold starts)."""
    need_seed = not os.path.exists(DATABASE_PATH) or os.path.getsize(DATABASE_PATH) == 0
    init_db()
    if need_seed:
        try:
            from init_db import seed_database
            seed_database(force_reset=False)
        except Exception as e:
            pass

# -------------------------------------------------------------
# Atomic Concurrency Safe Ticket Reservation
# -------------------------------------------------------------
def reserve_tickets(user_id, ticket_type_id, quantity):
    """
    Atomically decreases available_quantity and creates a 10-minute reservation hold.
    Uses BEGIN IMMEDIATE to prevent race conditions or double booking under heavy concurrency.
    Returns: (success: bool, data_or_error_msg, reservation_token)
    """
    if quantity <= 0:
        return False, "Quantity must be greater than zero.", None

    conn = get_db_connection()
    try:
        # Acquire immediate write lock
        conn.execute("BEGIN IMMEDIATE")
        
        # Check sales cutoff date and event status
        event_check = conn.execute("""
            SELECT e.id as event_id, e.title, e.sales_cutoff_date, e.status, t.price, t.available_quantity
            FROM ticket_types t
            JOIN events e ON t.event_id = e.id
            WHERE t.id = ?
        """, (ticket_type_id,)).fetchone()

        if not event_check:
            conn.rollback()
            return False, "Ticket type or event not found.", None

        if event_check['status'] != 'PUBLISHED':
            conn.rollback()
            return False, f"Event is not available for booking (status: {event_check['status']}).", None

        # Check cutoff date
        try:
            cutoff = datetime.fromisoformat(event_check['sales_cutoff_date'].replace(' ', 'T'))
            if utc_now() > cutoff:
                conn.rollback()
                return False, "Ticket sales for this event have closed.", None
        except Exception:
            pass

        # Conditional update: only decrease if available_quantity >= requested quantity
        cursor = conn.execute("""
            UPDATE ticket_types
            SET available_quantity = available_quantity - ?
            WHERE id = ? AND available_quantity >= ?
        """, (quantity, ticket_type_id, quantity))

        if cursor.rowcount == 0:
            conn.rollback()
            return False, f"Insufficient tickets available. Only {event_check['available_quantity']} left in stock.", None

        # Generate reservation token and 10-minute hold window
        token = str(uuid.uuid4())
        created_at = utc_now()
        expires_at = created_at + timedelta(minutes=10)
        
        unit_price = float(event_check['price'])
        total_amount = round(unit_price * quantity, 2)

        res_cursor = conn.execute("""
            INSERT INTO reservations (
                reservation_token, user_id, ticket_type_id, quantity,
                unit_price, total_amount, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
        """, (token, user_id, ticket_type_id, quantity, unit_price, total_amount, 
              created_at.strftime("%Y-%m-%d %H:%M:%S"), expires_at.strftime("%Y-%m-%d %H:%M:%S")))

        reservation_id = res_cursor.lastrowid
        conn.commit()

        return True, {
            "reservation_id": reservation_id,
            "token": token,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": total_amount,
            "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S")
        }, token

    except sqlite3.OperationalError as e:
        conn.rollback()
        return False, f"System busy handling requests: {str(e)}. Please retry.", None
    except Exception as e:
        conn.rollback()
        return False, f"Failed to reserve tickets: {str(e)}", None
    finally:
        conn.close()

# -------------------------------------------------------------
# Atomic Mock Payment & Booking Finalization
# -------------------------------------------------------------
def process_checkout(reservation_token, user_id, payment_method, force_outcome=None):
    """
    Processes checkout for a reservation.
    force_outcome can be 'SUCCESS' or 'FAILED' for deterministic testing/demo,
    or None for simulated random outcome (88% success rate).
    Returns (success: bool, result_dict_or_error)
    """
    import random
    cleanup_expired_reservations()

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Fetch reservation details
        res = conn.execute("""
            SELECT r.*, t.event_id, t.name as ticket_name, e.title as event_title
            FROM reservations r
            JOIN ticket_types t ON r.ticket_type_id = t.id
            JOIN events e ON t.event_id = e.id
            WHERE r.reservation_token = ?
        """, (reservation_token,)).fetchone()

        if not res:
            conn.rollback()
            return False, "Reservation not found."

        if res['user_id'] != user_id:
            conn.rollback()
            return False, "Unauthorized reservation checkout."

        if res['status'] == 'CONFIRMED':
            conn.rollback()
            return False, "This reservation has already been confirmed and paid."

        if res['status'] in ('EXPIRED', 'CANCELLED'):
            conn.rollback()
            return False, f"Reservation is no longer active (Status: {res['status']})."

        # Check expiration time
        expires_at = datetime.fromisoformat(res['expires_at'].replace(' ', 'T'))
        if utc_now() > expires_at:
            # Mark expired and restock
            conn.execute("UPDATE reservations SET status = 'EXPIRED' WHERE id = ?", (res['id'],))
            conn.execute("""
                UPDATE ticket_types
                SET available_quantity = available_quantity + ?
                WHERE id = ?
            """, (res['quantity'], res['ticket_type_id']))
            conn.commit()
            return False, "Your 10-minute hold has expired. The tickets have been released back to inventory."

        # Simulate Payment
        if force_outcome == 'SUCCESS':
            payment_success = True
            failure_reason = None
        elif force_outcome == 'FAILED':
            payment_success = False
            failure_reason = "Card declined: Insufficient funds or simulated card verification failure."
        else:
            payment_success = random.random() < 0.88
            failure_reason = None if payment_success else "Payment gateway timeout or bank decline."

        txn_ref = f"TXN-{uuid.uuid4().hex[:10].upper()}"

        if not payment_success:
            conn.execute("""
                INSERT INTO transactions (
                    transaction_reference, reservation_id, user_id, amount,
                    payment_method, status, failure_reason
                ) VALUES (?, ?, ?, ?, ?, 'FAILED', ?)
            """, (txn_ref, res['id'], user_id, res['total_amount'], payment_method, failure_reason))

            conn.execute("UPDATE reservations SET status = 'CANCELLED' WHERE id = ?", (res['id'],))
            conn.execute("""
                UPDATE ticket_types
                SET available_quantity = available_quantity + ?
                WHERE id = ?
            """, (res['quantity'], res['ticket_type_id']))

            conn.commit()
            return False, f"Payment Failed: {failure_reason}. Tickets have been restored to available inventory."

        # Payment Successful: Create Confirmed Booking
        booking_ref = f"BK-{uuid.uuid4().hex[:8].upper()}"

        booking_cursor = conn.execute("""
            INSERT INTO bookings (
                booking_reference, reservation_id, user_id, event_id,
                ticket_type_id, quantity, total_amount, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED')
        """, (booking_ref, res['id'], user_id, res['event_id'], 
              res['ticket_type_id'], res['quantity'], res['total_amount']))

        booking_id = booking_cursor.lastrowid

        conn.execute("UPDATE reservations SET status = 'CONFIRMED' WHERE id = ?", (res['id'],))

        conn.execute("""
            INSERT INTO transactions (
                transaction_reference, booking_id, reservation_id, user_id,
                amount, payment_method, status, failure_reason
            ) VALUES (?, ?, ?, ?, ?, ?, 'SUCCESS', NULL)
        """, (txn_ref, booking_id, res['id'], user_id, res['total_amount'], payment_method))

        conn.commit()

        return True, {
            "booking_reference": booking_ref,
            "booking_id": booking_id,
            "transaction_reference": txn_ref,
            "event_title": res['event_title'],
            "ticket_name": res['ticket_name'],
            "quantity": res['quantity'],
            "total_amount": res['total_amount']
        }

    except Exception as e:
        conn.rollback()
        return False, f"Checkout error: {str(e)}"
    finally:
        conn.close()

# -------------------------------------------------------------
# Cancel Reservation Manually
# -------------------------------------------------------------
def cancel_reservation(reservation_token, user_id):
    """Allows user to deliberately release their held tickets early."""
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        res = conn.execute("""
            SELECT id, user_id, ticket_type_id, quantity, status
            FROM reservations
            WHERE reservation_token = ?
        """, (reservation_token,)).fetchone()

        if not res:
            conn.rollback()
            return False, "Reservation not found."

        if res['user_id'] != user_id:
            conn.rollback()
            return False, "Unauthorized action."

        if res['status'] != 'PENDING':
            conn.rollback()
            return False, f"Reservation cannot be cancelled (status: {res['status']})."

        conn.execute("UPDATE reservations SET status = 'CANCELLED' WHERE id = ?", (res['id'],))
        conn.execute("""
            UPDATE ticket_types
            SET available_quantity = available_quantity + ?
            WHERE id = ?
        """, (res['quantity'], res['ticket_type_id']))

        conn.commit()
        return True, "Reservation cancelled and tickets released."
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()

# -------------------------------------------------------------
# Background & Scheduled Expired Reservations Cleaner
# -------------------------------------------------------------
def cleanup_expired_reservations():
    """
    Finds all PENDING reservations whose expires_at is past UTC now,
    marks them EXPIRED, and atomically restores available_quantity.
    Safe to call concurrently from background thread or inline handlers.
    """
    conn = get_db_connection()
    restored_count = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_str = utc_now().strftime("%Y-%m-%d %H:%M:%S")
        
        expired_rows = conn.execute("""
            SELECT id, ticket_type_id, quantity
            FROM reservations
            WHERE status = 'PENDING' AND expires_at < ?
        """, (now_str,)).fetchall()

        for row in expired_rows:
            conn.execute("UPDATE reservations SET status = 'EXPIRED' WHERE id = ?", (row['id'],))
            conn.execute("""
                UPDATE ticket_types
                SET available_quantity = available_quantity + ?
                WHERE id = ?
            """, (row['quantity'], row['ticket_type_id']))
            restored_count += 1

        conn.commit()
        return restored_count
    except Exception as e:
        conn.rollback()
        return 0
    finally:
        conn.close()

def start_background_cleanup_worker(interval_seconds=30):
    """Runs a daemon thread that executes cleanup_expired_reservations periodically."""
    def worker_loop():
        while True:
            try:
                time.sleep(interval_seconds)
                restored = cleanup_expired_reservations()
                if restored > 0:
                    print(f"[Worker] Auto-released {restored} expired reservation holds.")
            except Exception as e:
                pass

    thread = threading.Thread(target=worker_loop, daemon=True)
    thread.start()
    return thread
