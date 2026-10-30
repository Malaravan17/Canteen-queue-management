# Smart Canteen — Complete SQLite + React app

This bundle connects the React student menu/cart/checkout and manager dashboard to the FastAPI + SQLite backend. No MongoDB is required.

## Start backend (PowerShell terminal 1)
```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

If you already have a backend `.env`, keep it and do not overwrite it.

## Start frontend (PowerShell terminal 2)
```powershell
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```

Open http://localhost:5173. Manager dashboard: http://localhost:5173/admin.
The Vite proxy forwards `/api` requests to FastAPI at port 8000.

## Included
- Searchable menu with category filters and stock availability
- Cart quantity controls, subtotal, combo offers, checkout and customer name
- Counter assignment after order creation
- Cash payment recording and simulated online payment (no real money is charged)
- Order confirmation and live order status tracking
- Manager dashboard for order status, inventory updates, adding menu items, counters, analytics, worker alerts, and peak-mode settings
- QR code on the landing page pointing to the current site URL
- SQLite persistence in `backend/smartcanteen.db`

## Notes
- The backend uses SQLite only; no MongoDB server is required.
- Online payments are simulated for demonstration.
- Forecasting/restock suggestions are simple baselines, not a trained ML model.
- For phone testing, connect phone and PC to the same Wi-Fi, open `http://YOUR_PC_IPV4:5173` on the phone, and make sure the Vite terminal is started with `--host 0.0.0.0`. The QR will then point to the PC's IP URL.
