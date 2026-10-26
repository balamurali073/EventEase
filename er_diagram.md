# EventEase Database Architecture & Entity Relationship Diagram

This document illustrates the relational schema, constraints, indexes, and concurrency guarantees implemented in **EventEase – Multi-Tenant Event & Ticket Reservation System**.

---

## 1. Mermaid Entity Relationship (ER) Diagram

```mermaid
erDiagram
    USERS ||--o{ EVENTS : organizes
    USERS ||--o{ RESERVATIONS : holds
    USERS ||--o{ BOOKINGS : owns
    USERS ||--o{ TRANSACTIONS : pays

    EVENTS ||--|{ TICKET_TYPES : defines
    EVENTS ||--o{ BOOKINGS : has

    TICKET_TYPES ||--o{ RESERVATIONS : reserves
    TICKET_TYPES ||--o{ BOOKINGS : contains

    RESERVATIONS ||--o| BOOKINGS : confirms_into
    RESERVATIONS ||--o{ TRANSACTIONS : attempts

    BOOKINGS ||--o| TRANSACTIONS : settled_by

    USERS {
        INTEGER id PK "Auto Increment"
        TEXT username "Unique, Not Null"
        TEXT email "Unique, Not Null"
        TEXT password_hash "Werkzeug Scrypt/PBKDF2"
        TEXT role "CHECK ('admin', 'organizer', 'attendee')"
        INTEGER is_active "Default 1"
        TIMESTAMP created_at "UTC"
    }

    EVENTS {
        INTEGER id PK "Auto Increment"
        INTEGER organizer_id FK "References USERS(id)"
        TEXT title "Not Null"
        TEXT description "Not Null"
        TEXT category "Tech, Music, Business, Food, etc."
        TEXT venue "Location / Venue Name"
        TEXT event_date "ISO8601 String"
        TEXT sales_cutoff_date "ISO8601 String"
        TEXT banner_url "Image URL"
        TEXT status "CHECK ('DRAFT', 'PUBLISHED', 'CANCELLED', 'COMPLETED')"
        TIMESTAMP created_at "UTC"
    }

    TICKET_TYPES {
        INTEGER id PK "Auto Increment"
        INTEGER event_id FK "References EVENTS(id) ON DELETE CASCADE"
        TEXT name "VIP, General Admission, Early Bird"
        REAL price "CHECK (price >= 0)"
        INTEGER total_quantity "Total Allocated Inventory"
        INTEGER available_quantity "CHECK (available_quantity >= 0)"
        TIMESTAMP created_at "UTC"
    }

    RESERVATIONS {
        INTEGER id PK "Auto Increment"
        TEXT reservation_token "Unique UUID v4"
        INTEGER user_id FK "References USERS(id)"
        INTEGER ticket_type_id FK "References TICKET_TYPES(id)"
        INTEGER quantity "CHECK (quantity > 0)"
        REAL unit_price "Price at Hold Creation"
        REAL total_amount "Quantity * unit_price"
        TEXT status "CHECK ('PENDING', 'CONFIRMED', 'EXPIRED', 'CANCELLED')"
        TIMESTAMP created_at "UTC Hold Start"
        TIMESTAMP expires_at "UTC + 10 Minutes Hold Expiry"
    }

    BOOKINGS {
        INTEGER id PK "Auto Increment"
        TEXT booking_reference "Unique Reference (e.g. BK-9A4F2C1E)"
        INTEGER reservation_id FK "Unique References RESERVATIONS(id)"
        INTEGER user_id FK "References USERS(id)"
        INTEGER event_id FK "References EVENTS(id)"
        INTEGER ticket_type_id FK "References TICKET_TYPES(id)"
        INTEGER quantity "Confirmed Tickets"
        REAL total_amount "Settled Gross Amount"
        TEXT status "CHECK ('CONFIRMED', 'CANCELLED', 'REFUNDED')"
        TIMESTAMP created_at "UTC Confirmation Time"
    }

    TRANSACTIONS {
        INTEGER id PK "Auto Increment"
        TEXT transaction_reference "Unique Reference (e.g. TXN-81D92C4B)"
        INTEGER booking_id FK "Nullable References BOOKINGS(id)"
        INTEGER reservation_id FK "References RESERVATIONS(id)"
        INTEGER user_id FK "References USERS(id)"
        REAL amount "Transaction Total"
        TEXT payment_method "Credit Card, Debit Card, Digital Wallet"
        TEXT status "CHECK ('SUCCESS', 'FAILED')"
        TEXT failure_reason "Nullable Failure Diagnostics"
        TIMESTAMP created_at "UTC Payment Attempt"
    }
```

---

## 2. Table Schemas & Constraints Overview

| Table | Primary Key | Foreign Keys | Key Constraints & Integrity Rules |
| :--- | :--- | :--- | :--- |
| `users` | `id` | None | Unique `username`, unique `email`, role restricted to `admin`, `organizer`, `attendee`. |
| `events` | `id` | `organizer_id` &rarr; `users(id)` | Cascading deletes, valid status constraint (`PUBLISHED`, `DRAFT`, `CANCELLED`). |
| `ticket_types` | `id` | `event_id` &rarr; `events(id)` | Non-negative constraint `available_quantity >= 0`, `price >= 0`. |
| `reservations` | `id` | `user_id`, `ticket_type_id` | 10-minute hold window, `status IN ('PENDING', 'CONFIRMED', 'EXPIRED', 'CANCELLED')`. |
| `bookings` | `id` | `reservation_id`, `user_id`, `event_id`, `ticket_type_id` | Unique `booking_reference`, `reservation_id` 1-to-1 confirmation relationship. |
| `transactions` | `id` | `booking_id`, `reservation_id`, `user_id` | Full audit trail with status (`SUCCESS` / `FAILED`) and diagnostics. |

---

## 3. High Concurrency & Inventory Protection Mechanism

```
Attendee Request (Quantity: Q)
              │
              ▼
   [BEGIN IMMEDIATE Transaction]  <-- Locks SQLite write mutex immediately
              │
              ▼
   [Atomic Conditional UPDATE]
   UPDATE ticket_types
   SET available_quantity = available_quantity - Q
   WHERE id = ? AND available_quantity >= Q;
              │
        Rows Affected?
       ┌──────┴──────┐
   (== 1)          (== 0)
     │               │
     │               ▼
     │        [ROLLBACK] & Return "Insufficient Tickets"
     ▼
[INSERT INTO reservations (expires_at = now + 10m)]
     │
     ▼
 [COMMIT] & Return Hold Token
```
