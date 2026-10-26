# EventEase – Multi-Tenant Event & Ticket Reservation System

EventEase is a complete, beginner-friendly web application designed to solve high-concurrency ticket reservations, multi-tenant role management, and double-booking prevention.

Built with **Python Flask**, **SQLite (WAL Mode & Immediate Locking)**, **Vanilla CSS (Dark Navy & Purple theme)**, and **Chart.js**, EventEase provides an intuitive interface for attendees, organizers, and administrators.

---

## 1. Key Features & Highlights

- **Multi-Tenant Role-Based Access Control (RBAC):**
  - **Attendee:** Discover events by keyword & category, reserve tickets with temporary holds, view real-time countdowns, simulate mock checkout, and view digital tickets with unique IDs and QR passes.
  - **Organizer:** Publish and edit events, configure multi-tier ticket categories (e.g., *VIP*, *General Admission*, *Early Bird*), monitor ticket inventory, view attendee rosters, and analyze revenue trends via Chart.js.
  - **Admin:** Platform governance console with user account activation/deactivation, event status moderation, platform KPIs, and payment gateway audit logs.
- **Double-Booking & Overselling Prevention:**
  - Database transactions using SQLite `BEGIN IMMEDIATE` and atomic conditional updates (`WHERE available_quantity >= ?`).
  - Strict non-negative inventory enforcement guarantees two concurrent users can never reserve the same remaining ticket.
- **10-Minute Temporary Hold System:**
  - Reservations hold inventory exclusively for 10 minutes.
  - Visual circular & pill timer with animated warning pulses as time runs low.
  - Automatic expiration and restocking via a daemon background worker thread running every 30 seconds, plus on-demand validation at checkout.
- **Interactive Evaluator Payment Simulation:**
  - Choose between *Realistic Gateway (88% Success)*, *Force Success (Confirmed Booking)*, or *Force Failure (Card Declined & Immediate Inventory Restock)*.

---

## 2. Project Architecture & File Structure

```
EventEase/
├── app.py                      # Main Flask application & routes (Web + REST APIs)
├── database.py                 # SQLite connection, atomic transactions & background cleaner
├── init_db.py                  # Database initialization & rich demo seed script
├── test_concurrency.py         # Automated multi-threaded test suite (5 critical tests)
├── requirements.txt            # Python dependencies
├── er_diagram.md               # Mermaid ER diagram and schema documentation
├── postman_collection.json     # Ready-to-import Postman v2.1 collection
├── README.md                   # Complete documentation and setup guide
├── static/
│   ├── css/
│   │   └── style.css           # Dark navy (#0B0F19) & royal purple (#7C3AED) responsive styling
│   └── js/
│       └── script.js           # Countdown timer, dynamic tier calculations & Chart.js renderer
└── templates/
    ├── base.html               # Main layout with glassmorphic navbar & flash toasts
    ├── index.html              # Event discovery, category chips & availability cards
    ├── login.html              # Sign in with role switcher helper
    ├── register.html           # Registration with role selection (Attendee/Organizer/Admin)
    ├── event_detail.html       # Event details & multi-tier ticket reservation form
    ├── checkout.html           # 10-minute hold timer & mock payment gateway
    ├── booking_confirmation.html # Confirmed pass with cryptographic ID & SVG QR badge
    ├── my_bookings.html        # Attendee order history & digital tickets
    ├── organizer_dashboard.html# Organizer KPIs & Chart.js revenue breakdown
    ├── create_event.html       # Event publishing with dynamic multi-tier ticket builder
    ├── edit_event.html         # Event updates & additional ticket tier additions
    ├── event_attendees.html    # Event attendee roster with export/print capability
    └── admin_dashboard.html    # Platform metrics, user toggles & transaction audit log
```

---

## 3. Getting Started on Windows (PowerShell / Command Prompt)

### Prerequisites
- **Python 3.10+** (Tested and verified on Python 3.13)
- Windows PowerShell or Command Prompt

### Step 1: Navigate to Project Directory
```powershell
cd c:\Users\Balamurali\OneDrive\Desktop\EventEase
```

### Step 2: Install Dependencies
```powershell
pip install -r requirements.txt
```

### Step 3: Initialize & Seed Demo Database
To create the database tables and populate sample events, ticket tiers, and demo accounts:
```powershell
python init_db.py --reset
```

### Step 4: Run Concurrency & Reliability Tests
Verify that double-booking prevention, insufficient stock, expiration release, and access controls pass:
```powershell
python test_concurrency.py
```
*Output: `ALL TESTS PASSED SUCCESSFULLY! (5 / 5)`*

### Step 5: Start the Web Server
```powershell
python app.py
```
The server will start at: **`http://127.0.0.1:5000`**

Open `http://127.0.0.1:5000` in your web browser.

---

## 4. Default Seeded Demo Accounts

You can log in directly using any of these seeded credentials:

| Role | Email / Username | Password | Access Scope |
| :--- | :--- | :--- | :--- |
| **Admin** | `admin@eventease.com` | `Admin@123` | Platform oversight, user activation, system metrics, transaction audit |
| **Organizer** | `organizer@eventease.com` | `Organizer@123` | Create events, configure ticket tiers, view revenue charts & attendees |
| **Attendee** | `attendee@eventease.com` | `Attendee@123` | Browse events, reserve tickets, checkout, view booked tickets |

*Note: You can also register a new account at `/register` with any role.*

---

## 5. Technical Deep-Dive: Concurrency & Double Booking Prevention

### The Race Condition Problem
When two attendees simultaneously try to reserve the last available ticket:
1. Thread A checks `available_quantity` &rarr; reads `1`.
2. Thread B checks `available_quantity` &rarr; reads `1`.
3. Thread A reserves ticket &rarr; decrements to `0`.
4. Thread B reserves ticket &rarr; decrements to `-1` (Oversold!).

### The EventEase Solution
EventEase solves this in SQLite through two layers:

1. **Write Transaction Isolation (`BEGIN IMMEDIATE`):**
   ```python
   conn.execute("BEGIN IMMEDIATE")
   ```
   This immediately acquires a reserved lock on the SQLite file, preventing any concurrent write transaction from obtaining stale state.

2. **Atomic Conditional Decrement:**
   ```sql
   UPDATE ticket_types
   SET available_quantity = available_quantity - ?
   WHERE id = ? AND available_quantity >= ?;
   ```
   If 10 threads attempt to reserve the last 3 tickets, the database engine checks `available_quantity >= ?` atomically at the moment of update. Exactly 3 threads update 1 row (`rowcount == 1`); the other 7 match 0 rows (`rowcount == 0`), triggering an immediate rollback and user-friendly notice: *"Insufficient tickets available"*.

3. **Safe Restocking on Expiration or Payment Failure:**
   ```sql
   UPDATE ticket_types
   SET available_quantity = available_quantity + ?
   WHERE id = ?;
   ```
   If payment fails, card is declined, or the 10-minute hold expires, tickets are automatically restored back to stock without human intervention.

---

## 6. Local Development vs. Production Architecture

| Requirement | Local Version (EventEase SQLite) | Enterprise Production Recommendation |
| :--- | :--- | :--- |
| **Database** | SQLite with WAL mode & busy timeout | PostgreSQL with row-level locks (`SELECT ... FOR UPDATE`) |
| **Cache & Locks** | In-memory atomic SQLite transactions | Redis Distributed Locks (`Redlock`) with millisecond TTL |
| **Hold Expiration** | Python daemon background worker thread | Redis Key Expiration Notifications (`__keyevent@0__:expired`) + Celery beat worker |
| **Payment Gateway**| Simulated mock gateway with deterministic force options | Stripe / Adyen webhooks with idempotent event handlers |
| **QR Generation**  | Pure scalable SVG vector barcode | Pre-rendered PNG signed with cryptographic HMAC token |

---

## 7. Automated Test Suite Overview (`test_concurrency.py`)

The included test runner validates 5 critical real-world scenarios:

1. **`test_concurrent_reservations`**: 10 simultaneous threads contend for 3 tickets. Validates that exactly 3 succeed, 7 fail, and final inventory is exactly 0 (never negative).
2. **`test_insufficient_inventory`**: Requesting 10 tickets when only 5 exist is rejected; inventory remains untouched.
3. **`test_expired_holds`**: Simulates a hold created 15 minutes ago. Checkout rejects it, marks it `EXPIRED`, and restores the tickets.
4. **`test_failed_payment_rollback`**: Simulates a card decline. Verifies that the reservation is marked `CANCELLED`, audit transaction is logged, and tickets return to the pool.
5. **`test_unauthorized_access`**: Ensures unauthenticated or attendee accounts cannot access organizer dashboard or admin console.

---

## 8. Postman Collection Testing

A complete Postman v2.1 collection is included at [`postman_collection.json`](postman_collection.json).

### How to Import:
1. Open Postman.
2. Click **Import** &rarr; Select `postman_collection.json`.
3. The collection is configured with `{{baseUrl}}` (`http://127.0.0.1:5000`) and includes tests that automatically capture the reservation token for the checkout request!
