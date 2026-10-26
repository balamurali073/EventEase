"""
EventEase Automated Concurrency & Reliability Test Suite
Covers:
1. Concurrent multi-threaded reservations (Double booking prevention & inventory integrity)
2. Insufficient inventory handling
3. Expired hold cleanup & checkout rejection
4. Mock payment failure & atomic rollback
5. Role-based unauthorized access checks
"""

import threading
import time
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from werkzeug.security import generate_password_hash
from database import (
    get_db_connection, init_db, reserve_tickets,
    process_checkout, cleanup_expired_reservations, utc_now
)
from app import app

def setup_isolated_test_db():
    """Sets up a clean test database state."""
    init_db()
    conn = get_db_connection()
    # Clean up test rows
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM bookings")
    conn.execute("DELETE FROM reservations")
    conn.execute("DELETE FROM ticket_types")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM users")

    # Insert test users
    pw_hash = generate_password_hash("TestPass@123")
    conn.execute("INSERT INTO users (id, username, email, password_hash, role) VALUES (101, 'test_admin', 'admin@test.com', ?, 'admin')", (pw_hash,))
    conn.execute("INSERT INTO users (id, username, email, password_hash, role) VALUES (102, 'test_organizer', 'org@test.com', ?, 'organizer')", (pw_hash,))
    conn.execute("INSERT INTO users (id, username, email, password_hash, role) VALUES (103, 'test_attendee', 'att@test.com', ?, 'attendee')", (pw_hash,))
    conn.execute("INSERT INTO users (id, username, email, password_hash, role) VALUES (104, 'test_attendee_2', 'att2@test.com', ?, 'attendee')", (pw_hash,))

    # Insert test event
    now = utc_now()
    event_date = (now + timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    cutoff_date = (now + timedelta(days=9)).strftime("%Y-%m-%d %H:%M")
    conn.execute("""
        INSERT INTO events (id, organizer_id, title, description, category, venue, event_date, sales_cutoff_date, status)
        VALUES (501, 102, 'Concurrency Con 2026', 'Testing race conditions', 'Technology', 'Test Arena', ?, ?, 'PUBLISHED')
    """, (event_date, cutoff_date))

    conn.commit()
    conn.close()

# -------------------------------------------------------------
# 1. Test Concurrent Reservations (Double-Booking Prevention)
# -------------------------------------------------------------
def test_concurrent_reservations():
    print("\n--- Test 1: Concurrent Reservations (Double-Booking Prevention) ---")
    setup_isolated_test_db()
    conn = get_db_connection()

    # Create ticket tier with exactly 3 tickets
    conn.execute("""
        INSERT INTO ticket_types (id, event_id, name, price, total_quantity, available_quantity)
        VALUES (901, 501, 'Limited Tier', 50.00, 3, 3)
    """)
    conn.commit()
    conn.close()

    results = []
    threads = []
    num_threads = 10  # 10 concurrent requests fighting for 3 tickets

    def attempt_reservation(thread_idx):
        user_id = 103 if thread_idx % 2 == 0 else 104
        success, res, token = reserve_tickets(user_id, ticket_type_id=901, quantity=1)
        results.append((success, res, token))

    # Launch threads concurrently
    for i in range(num_threads):
        t = threading.Thread(target=attempt_reservation, args=(i,))
        threads.append(t)

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    success_count = sum(1 for s, r, tok in results if s is True)
    fail_count = sum(1 for s, r, tok in results if s is False)

    conn = get_db_connection()
    ticket_stock = conn.execute("SELECT available_quantity FROM ticket_types WHERE id = 901").fetchone()[0]
    res_count = conn.execute("SELECT COUNT(*) FROM reservations WHERE ticket_type_id = 901 AND status = 'PENDING'").fetchone()[0]
    conn.close()

    print(f"Concurrent results: {success_count} succeeded, {fail_count} failed out of {num_threads} attempts.")
    print(f"Final available stock in database: {ticket_stock}")
    print(f"Pending reservations created: {res_count}")

    assert success_count == 3, f"Expected exactly 3 successful reservations, got {success_count}"
    assert fail_count == 7, f"Expected 7 rejections, got {fail_count}"
    assert ticket_stock == 0, f"Stock must be exactly 0, got {ticket_stock}"
    assert ticket_stock >= 0, "Inventory must NEVER drop below 0!"
    print("PASS: Concurrency test verified. Zero double bookings; inventory remained non-negative.")

# -------------------------------------------------------------
# 2. Test Insufficient Inventory
# -------------------------------------------------------------
def test_insufficient_inventory():
    print("\n--- Test 2: Insufficient Inventory Handling ---")
    setup_isolated_test_db()
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO ticket_types (id, event_id, name, price, total_quantity, available_quantity)
        VALUES (902, 501, 'Small Batch', 30.00, 5, 5)
    """)
    conn.commit()
    conn.close()

    # Attempt to reserve 10 tickets when only 5 are in stock
    success, msg, token = reserve_tickets(user_id=103, ticket_type_id=902, quantity=10)
    print(f"Attempt result: success={success}, msg='{msg}'")

    assert success is False, "Reservation should have failed due to insufficient stock."
    assert "Insufficient tickets" in msg

    conn = get_db_connection()
    stock = conn.execute("SELECT available_quantity FROM ticket_types WHERE id = 902").fetchone()[0]
    conn.close()

    assert stock == 5, f"Stock must remain untouched at 5, found {stock}"
    print("PASS: Insufficient inventory successfully prevented and stock preserved.")

# -------------------------------------------------------------
# 3. Test Expired Holds & Checkout Rejection
# -------------------------------------------------------------
def test_expired_holds():
    print("\n--- Test 3: Expired Hold Cleanup & Checkout Rejection ---")
    setup_isolated_test_db()
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO ticket_types (id, event_id, name, price, total_quantity, available_quantity)
        VALUES (903, 501, 'Timed Tier', 40.00, 10, 10)
    """)
    conn.commit()
    conn.close()

    # Reserve 2 tickets
    success, res, token = reserve_tickets(user_id=103, ticket_type_id=903, quantity=2)
    assert success is True, "Initial reservation should succeed."

    # Verify inventory decreased to 8
    conn = get_db_connection()
    stock_after_reserve = conn.execute("SELECT available_quantity FROM ticket_types WHERE id = 903").fetchone()[0]
    assert stock_after_reserve == 8, f"Expected 8 tickets, got {stock_after_reserve}"

    # Artificially expire the reservation by setting expires_at to 15 minutes ago
    past_time = (utc_now() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE reservations SET expires_at = ? WHERE reservation_token = ?", (past_time, token))
    conn.commit()
    conn.close()

    # Attempt checkout on the expired reservation
    checkout_success, checkout_msg = process_checkout(token, user_id=103, payment_method="Credit Card")
    print(f"Expired checkout result: success={checkout_success}, msg='{checkout_msg}'")

    assert checkout_success is False, "Expired reservation checkout must be rejected."
    assert "expired" in checkout_msg.lower()

    # Verify stock restored back to 10
    conn = get_db_connection()
    final_stock = conn.execute("SELECT available_quantity FROM ticket_types WHERE id = 903").fetchone()[0]
    res_status = conn.execute("SELECT status FROM reservations WHERE reservation_token = ?", (token,)).fetchone()[0]
    conn.close()

    print(f"Restored stock: {final_stock}, Reservation status: {res_status}")
    assert final_stock == 10, f"Stock must be restored to 10, found {final_stock}"
    assert res_status == 'EXPIRED', f"Reservation status should be EXPIRED, got {res_status}"
    print("PASS: Expired hold automatically released tickets and rejected checkout.")

# -------------------------------------------------------------
# 4. Test Failed Payment & Atomic Rollback
# -------------------------------------------------------------
def test_failed_payment_rollback():
    print("\n--- Test 4: Failed Payment & Atomic Rollback ---")
    setup_isolated_test_db()
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO ticket_types (id, event_id, name, price, total_quantity, available_quantity)
        VALUES (904, 501, 'Payment Test Tier', 60.00, 10, 10)
    """)
    conn.commit()
    conn.close()

    # Reserve 3 tickets (stock becomes 7)
    success, res, token = reserve_tickets(user_id=103, ticket_type_id=904, quantity=3)
    assert success is True

    conn = get_db_connection()
    stock_during_hold = conn.execute("SELECT available_quantity FROM ticket_types WHERE id = 904").fetchone()[0]
    assert stock_during_hold == 7

    # Force payment failure
    checkout_success, checkout_msg = process_checkout(
        token, user_id=103, payment_method="Debit Card", force_outcome='FAILED'
    )
    print(f"Forced payment failure result: success={checkout_success}, msg='{checkout_msg}'")

    assert checkout_success is False
    assert "Payment Failed" in checkout_msg

    # Verify tickets restored to 10 and transaction logged
    restored_stock = conn.execute("SELECT available_quantity FROM ticket_types WHERE id = 904").fetchone()[0]
    res_status = conn.execute("SELECT status FROM reservations WHERE reservation_token = ?", (token,)).fetchone()[0]
    failed_txn = conn.execute("SELECT status, failure_reason FROM transactions WHERE user_id = 103").fetchone()
    bookings_count = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    conn.close()

    assert restored_stock == 10, f"Stock must return to 10 after failed payment, found {restored_stock}"
    assert res_status == 'CANCELLED', f"Reservation status should be CANCELLED, got {res_status}"
    assert failed_txn['status'] == 'FAILED'
    assert bookings_count == 0, "No confirmed bookings should exist after failed payment"
    print("PASS: Failed payment rolled back inventory and recorded audit log.")

# -------------------------------------------------------------
# 5. Test Role-Based Unauthorized Access
# -------------------------------------------------------------
def test_unauthorized_access():
    print("\n--- Test 5: Role-Based Unauthorized Access Control ---")
    client = app.test_client()

    # 1. Unauthenticated access to organizer dashboard -> redirects to login
    resp = client.get('/organizer/dashboard')
    assert resp.status_code == 302
    assert '/login' in resp.headers.get('Location', '')

    # 2. Unauthenticated access to admin dashboard -> redirects to login
    resp = client.get('/admin/dashboard')
    assert resp.status_code == 302
    assert '/login' in resp.headers.get('Location', '')

    # 3. Authenticate as Attendee
    with client.session_transaction() as sess:
        sess['user_id'] = 103
        sess['username'] = 'test_attendee'
        sess['role'] = 'attendee'

    # Attendee accessing admin dashboard -> forbidden / redirected to index
    resp = client.get('/admin/dashboard')
    assert resp.status_code == 302
    assert resp.headers.get('Location') in ('/', 'http://localhost/')

    # Attendee accessing organizer dashboard -> forbidden / redirected to index
    resp = client.get('/organizer/dashboard')
    assert resp.status_code == 302
    assert resp.headers.get('Location') in ('/', 'http://localhost/')

    # Attendee accessing My Bookings -> OK (200)
    resp = client.get('/my-bookings')
    assert resp.status_code == 200

    print("PASS: Role-based access control strictly enforced.")

# -------------------------------------------------------------
# Main Test Runner
# -------------------------------------------------------------
if __name__ == "__main__":
    print("==============================================================")
    print("EventEase - Starting Comprehensive Test Suite")
    print("==============================================================")
    test_concurrent_reservations()
    test_insufficient_inventory()
    test_expired_holds()
    test_failed_payment_rollback()
    test_unauthorized_access()
    print("\n==============================================================")
    print("ALL TESTS PASSED SUCCESSFULLY! (5 / 5)")
    print("==============================================================")
