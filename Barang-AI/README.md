# Barang-AI

## Project structure

- `frontend/` contains the browser application: HTML, CSS, JavaScript, and images.
- `backend/` contains the Python API server, SQLite database, and SMTP configuration.

## Run the SQLite-backed app

1. Install Python 3.8 or newer.
2. From this folder, run:

   ```powershell
   python backend/server.py
   ```

3. Open http://localhost:8000.

The server creates `backend/barang-ai.db` automatically. It stores resident accounts, transactions, chat messages, and login sessions in SQLite, so restarting the server does not invalidate existing login sessions. There is no seeded admin or staff account in the application.

Signup verification emails use SMTP. Configure these environment variables before starting the server:

```powershell
$env:BARANGAI_SMTP_HOST = "smtp.example.com"
$env:BARANGAI_SMTP_PORT = "587"
$env:BARANGAI_SMTP_USERNAME = "your-smtp-username"
$env:BARANGAI_SMTP_PASSWORD = "your-smtp-password"
$env:BARANGAI_SMTP_FROM = "Barang-AI <no-reply@example.com>"
```

New accounts must verify the six-digit code sent to their email within one minute before the account is created.

Resident assistant messages are sent through the configured n8n chat webhook by the server. The browser calls the same-origin local `/api/ai/chat` endpoint, which forwards the message and session context to n8n. Staff management routes are not exposed, and the server does not allow cross-origin API requests.

## Utility borrowing API

The backend creates these SQLite tables:

- `utility_items`: inventory items with a total quantity and active flag.
- `borrow_requests`: resident requests with resident name, contact number, barangay ID, quantity, purpose, return date/date range, and status (`pending`, `approved`, `borrowed`, `returned`, `rejected`, or `cancelled`).

Authenticated residents can use:

- `GET /api/utility-items?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD` to see live availability for a date range.
- `POST /api/borrow-requests` with `itemId`, `residentName`, `contactNumber`, `barangayId`, `quantity`, `purpose`, `startDate`, and `endDate` to submit a request.
- `GET /api/borrow-requests` to view their submitted requests.

Pending, approved, and borrowed requests reserve inventory for overlapping dates. Submissions are checked in a database write transaction to prevent overbooking.
