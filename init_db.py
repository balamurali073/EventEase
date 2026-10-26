import sqlite3
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from werkzeug.security import generate_password_hash
from database import get_db_connection, init_db, DATABASE_PATH, utc_now

def seed_database(force_reset=False):
    """Seeds the database with test accounts, realistic events, ticket tiers, and sample bookings."""
    if force_reset and os.path.exists(DATABASE_PATH):
        try:
            os.remove(DATABASE_PATH)
            # Also remove WAL and SHM if they exist
            for ext in ('-wal', '-shm'):
                wal_file = DATABASE_PATH + ext
                if os.path.exists(wal_file):
                    os.remove(wal_file)
            print("Existing database files removed for fresh re-seed.")
        except Exception as e:
            print(f"Note on resetting DB file: {e}")

    init_db()

    conn = get_db_connection()
    cursor = conn.cursor()

    # Check if already seeded
    user_count = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count > 0 and not force_reset:
        print("Database already contains data. Use --reset to reseed from scratch.")
        conn.close()
        return

    print("Seeding database with demo users, events, and ticket types...")

    # 1. Create Default Users (3 Roles)
    users_data = [
        ("admin", "admin@eventease.com", generate_password_hash("Admin@123"), "admin"),
        ("organizer", "organizer@eventease.com", generate_password_hash("Organizer@123"), "organizer"),
        ("tech_events", "events@technova.io", generate_password_hash("Organizer@123"), "organizer"),
        ("attendee", "attendee@eventease.com", generate_password_hash("Attendee@123"), "attendee"),
        ("sarah_connor", "sarah@example.com", generate_password_hash("Attendee@123"), "attendee"),
        ("alex_rivera", "alex@example.com", generate_password_hash("Attendee@123"), "attendee")
    ]

    cursor.executemany("""
        INSERT INTO users (username, email, password_hash, role)
        VALUES (?, ?, ?, ?)
    """, users_data)

    organizer_id = 2
    tech_organizer_id = 3
    attendee_id = 4
    sarah_id = 5

    # 2. Seed Realistic Events
    now = utc_now()
    events_data = [
        (
            organizer_id,
            "Global AI & Cloud Innovation Summit 2026",
            "Join world-class engineering leaders, researchers, and innovators discussing the frontier of Generative AI, Distributed Systems, and Next-Gen Cloud Architecture. Features hands-on workshops and networking gala.",
            "Technology",
            "Moscone West & Grand Ballroom, San Francisco, CA",
            (now + timedelta(days=20)).strftime("%Y-%m-%d 09:00"),
            (now + timedelta(days=19)).strftime("%Y-%m-%d 23:59"),
            "https://images.unsplash.com/photo-1540575467063-178a50c2df87?auto=format&fit=crop&w=1200&q=80",
            "PUBLISHED"
        ),
        (
            organizer_id,
            "Neon Horizon Electronic Music Festival",
            "An electrifying 2-night audiovisual spectacle featuring world-renowned electronic DJs, 3D laser projection mapping, and immersive soundscapes under the stars.",
            "Music",
            "Bayfront Amphitheater, Miami, FL",
            (now + timedelta(days=14)).strftime("%Y-%m-%d 18:00"),
            (now + timedelta(days=13)).strftime("%Y-%m-%d 23:59"),
            "https://images.unsplash.com/photo-1470225620780-dba8ba36b745?auto=format&fit=crop&w=1200&q=80",
            "PUBLISHED"
        ),
        (
            tech_organizer_id,
            "Fintech Horizons & Venture Capital Forum",
            "High-impact keynotes and fireside chats with premier founders, angel syndicates, and financial institution architects exploring decentralized finance and regulatory trends.",
            "Business",
            "Metropolitan Club, New York, NY",
            (now + timedelta(days=35)).strftime("%Y-%m-%d 10:00"),
            (now + timedelta(days=33)).strftime("%Y-%m-%d 18:00"),
            "https://images.unsplash.com/photo-1515187029135-18ee286d815b?auto=format&fit=crop&w=1200&q=80",
            "PUBLISHED"
        ),
        (
            organizer_id,
            "Modern Gastronomy & Artisanal Wine Expo",
            "Indulge in culinary masterclasses with Michelin-starred chefs, rare vintage wine tastings, and sustainable farm-to-table culinary exhibits.",
            "Food & Drink",
            "Pier 48 Maritime Center, Seattle, WA",
            (now + timedelta(days=45)).strftime("%Y-%m-%d 12:00"),
            (now + timedelta(days=44)).strftime("%Y-%m-%d 23:59"),
            "https://images.unsplash.com/photo-1510812431401-41d2bd2722f3?auto=format&fit=crop&w=1200&q=80",
            "PUBLISHED"
        ),
        (
            tech_organizer_id,
            "National Indie Game Developers Showcase",
            "Play unreleased indie games, meet independent developers, and participate in competitive speedrunning challenges and game development panels.",
            "Gaming",
            "Austin Convention Center, Austin, TX",
            (now + timedelta(days=28)).strftime("%Y-%m-%d 11:00"),
            (now + timedelta(days=27)).strftime("%Y-%m-%d 22:00"),
            "https://images.unsplash.com/photo-1511512578047-dfb367046420?auto=format&fit=crop&w=1200&q=80",
            "PUBLISHED"
        )
    ]

    for ev in events_data:
        cursor.execute("""
            INSERT INTO events (
                organizer_id, title, description, category, venue,
                event_date, sales_cutoff_date, banner_url, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ev)

    # 3. Seed Multi-Tier Ticket Types for Events
    tickets_data = [
        # Event 1: AI Summit
        (1, "Early Bird Pass", 149.00, 50, 42),
        (1, "General Admission", 249.00, 150, 138),
        (1, "VIP Platinum All-Access", 599.00, 30, 26),

        # Event 2: Music Festival
        (2, "General Lawn Entry", 75.00, 200, 185),
        (2, "Main Stage Pit Pass", 130.00, 80, 70),
        (2, "Backstage VIP Lounge", 320.00, 25, 22),

        # Event 3: Fintech Forum
        (3, "Standard Delegate", 350.00, 100, 94),
        (3, "Executive VIP & Private Dinner", 850.00, 20, 17),

        # Event 4: Gastronomy Expo
        (4, "Tasting Pass", 65.00, 120, 115),
        (4, "Chef Masterclass + VIP Wine", 160.00, 40, 38),

        # Event 5: Indie Game Expo
        (5, "Student Pass", 25.00, 100, 96),
        (5, "Weekend Expo Badge", 55.00, 150, 142)
    ]

    cursor.executemany("""
        INSERT INTO ticket_types (event_id, name, price, total_quantity, available_quantity)
        VALUES (?, ?, ?, ?, ?)
    """, tickets_data)

    # 4. Seed Historical Bookings & Transactions to populate Chart.js and dashboards
    sample_bookings = [
        (attendee_id, 1, 1, 2, 149.00), # AI Summit Early Bird
        (attendee_id, 2, 4, 3, 75.00),  # Music Fest General Lawn
        (sarah_id, 1, 3, 1, 599.00),     # AI Summit VIP
        (sarah_id, 3, 7, 1, 350.00)      # Fintech Standard
    ]

    for user, evt, t_type, qty, price in sample_bookings:
        total = round(qty * price, 2)
        res_token = str(uuid.uuid4())
        created_time = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        exp_time = (now - timedelta(days=2, minutes=-10)).strftime("%Y-%m-%d %H:%M:%S")

        res_cur = cursor.execute("""
            INSERT INTO reservations (
                reservation_token, user_id, ticket_type_id, quantity, unit_price,
                total_amount, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'CONFIRMED', ?, ?)
        """, (res_token, user, t_type, qty, price, total, created_time, exp_time))
        res_id = res_cur.lastrowid

        b_ref = f"BK-{uuid.uuid4().hex[:8].upper()}"
        b_cur = cursor.execute("""
            INSERT INTO bookings (
                booking_reference, reservation_id, user_id, event_id,
                ticket_type_id, quantity, total_amount, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?)
        """, (b_ref, res_id, user, evt, t_type, qty, total, created_time))
        booking_id = b_cur.lastrowid

        txn_ref = f"TXN-{uuid.uuid4().hex[:10].upper()}"
        cursor.execute("""
            INSERT INTO transactions (
                transaction_reference, booking_id, reservation_id, user_id,
                amount, payment_method, status, failure_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, 'Credit Card', 'SUCCESS', NULL, ?)
        """, (txn_ref, booking_id, res_id, user, total, created_time))

    conn.commit()
    conn.close()
    print("Database seeding completed with demo accounts and live event inventory!")

if __name__ == "__main__":
    reset = "--reset" in sys.argv
    seed_database(force_reset=reset)
