import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { QRCodeSVG } from "qrcode.react";
import "./style.css";

const API = "/api";
const money = (n) => `₹${Number(n || 0).toFixed(2)}`;
const prettyStatus = (s) => ({ queued: "Queued", preparing: "Preparing", ready: "Ready for pickup", completed: "Completed", cancelled: "Cancelled" }[s] || s);
const dateTime = (v) => v ? new Date(v).toLocaleString() : "—";

class AppErrorBoundary extends React.Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("Smart Canteen page failed to render:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return <main style={{maxWidth: 760, margin: "80px auto", padding: 24, fontFamily: "sans-serif", color: "#24352b"}}>
        <h1>Page could not be displayed</h1>
        <p>The app hit a rendering error. Refresh the page after correcting the issue.</p>
        <pre style={{whiteSpace: "pre-wrap", padding: 16, borderRadius: 8, background: "#fff0ef", color: "#8d2424"}}>{this.state.error.message}</pre>
      </main>;
    }
    return this.props.children;
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text || "Unexpected server response" }; }
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function App() {
  return window.location.pathname.startsWith("/admin") ? <ManagerPage /> : <CustomerPage />;
}

function Header({ admin = false, cartCount = 0, onCart }) {
  return <header className="topbar">
    <a href="/" className="brand">
      <span className="brand-mark">SQ</span>
      <span><small>CAMPUS DINING</small><strong>Smart Canteen</strong></span>
    </a>
    <nav>
      {admin
        ? <a className="nav-link" href="/">Student menu ↗</a>
        : <><a className="nav-link desktop-only" href="/admin">Manager ↗</a><button className="cart-nav" onClick={onCart}>🛒 Cart <b>{cartCount}</b></button></>}
    </nav>
  </header>;
}

function CustomerPage() {
  const [menu, setMenu] = useState([]);
  const [combos, setCombos] = useState([]);
  const [cart, setCart] = useState({});
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All");
  const [page, setPage] = useState("menu");
  const [paymentMethod, setPaymentMethod] = useState("cash");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [order, setOrder] = useState(null);
  const [orderIdToTrack, setOrderIdToTrack] = useState("");
  const [trackedOrder, setTrackedOrder] = useState(null);
  const [selectedCombo, setSelectedCombo] = useState("");
  const qrScanStarted = useRef(false);

  const loadMenu = useCallback(async () => {
    try {
      const [items, comboList] = await Promise.all([
        request("/items?available_only=true"),
        request("/combos"),
      ]);
      setMenu(items);
      setCombos(comboList);
      setError("");
    } catch (e) { setError(`Could not load menu: ${e.message}. Is the FastAPI backend running on port 8000?`); }
  }, []);

  useEffect(() => { loadMenu(); }, [loadMenu]);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get("qr_scan") !== "1" || qrScanStarted.current) return;

    qrScanStarted.current = true;
    request("/scan", { method: "POST", body: JSON.stringify({ name: "Student" }) })
      .then(({ customer }) => {
        url.searchParams.delete("qr_scan");
        window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
        setNotice(`QR check-in counted. Please join Counter ${customer.counter_id}. No food order has been placed.`);
      })
      .catch((e) => {
        qrScanStarted.current = false;
        setError(`QR check-in could not be registered: ${e.message}. Please refresh to retry.`);
      });
  }, []);

  const categories = useMemo(() => ["All", ...new Set(menu.map(i => i.category))], [menu]);
  const filteredMenu = useMemo(() => menu.filter(i =>
    (category === "All" || i.category === category) &&
    `${i.name} ${i.description} ${i.category}`.toLowerCase().includes(query.toLowerCase())
  ), [menu, category, query]);

  const cartLines = useMemo(() => Object.entries(cart)
    .map(([id, quantity]) => ({ item: menu.find(i => String(i.id) === id), quantity }))
    .filter(line => line.item && line.quantity > 0), [cart, menu]);
  const subtotal = cartLines.reduce((sum, line) => sum + Number(line.item.price) * line.quantity, 0);
  const cartCount = cartLines.reduce((sum, line) => sum + line.quantity, 0);
  const comboDiscount = useMemo(() => {
    if (!selectedCombo) return 0;
    const combo = combos.find(c => String(c.id) === String(selectedCombo));
    if (!combo) return 0;
    const names = new Set(cartLines.map(x => x.item.name));
    if (!(combo.item_names || []).every(n => names.has(n))) return 0;
    const eligible = cartLines.filter(x => combo.item_names.includes(x.item.name)).reduce((sum, x) => sum + Number(x.item.price) * x.quantity, 0);
    return Math.max(0, eligible - Number(combo.price));
  }, [selectedCombo, combos, cartLines]);
  const total = Math.max(0, subtotal - comboDiscount);

  function changeQty(itemId, delta) {
    setCart(old => {
      const next = { ...old };
      const key = String(itemId);
      const qty = Math.max(0, (next[key] || 0) + delta);
      if (qty === 0) delete next[key]; else next[key] = qty;
      return next;
    });
  }

  async function placeOrder(e) {
    e?.preventDefault();
    setError(""); setNotice("");
    if (!cartLines.length) { setError("Add at least one item to your cart."); return; }
    if (!name.trim()) { setError("Enter your name before placing the order."); return; }
    setBusy(true);
    try {
      const created = await request("/orders", {
        method: "POST",
        body: JSON.stringify({
          customer_name: name.trim(),
          items: cartLines.map(line => ({ item_id: String(line.item.id), quantity: line.quantity })),
          combo_id: selectedCombo || null,
        }),
      });
      const payment = await request(`/orders/${created.id}/payment`, {
        method: "POST",
        body: JSON.stringify({ method: paymentMethod, simulate_success: true }),
      });
      setOrder(payment.order || created);
      setTrackedOrder(payment.order || created);
      setCart({});
      setSelectedCombo("");
      setPage("confirmation");
      setNotice(payment.simulated ? "Demo online payment recorded (no real money was charged)." : "Order placed successfully.");
      await loadMenu();
    } catch (e2) {
      setError(e2.message);
      await loadMenu();
    } finally { setBusy(false); }
  }

  async function trackOrder(e) {
    e?.preventDefault();
    setError(""); setTrackedOrder(null);
    if (!orderIdToTrack.trim()) { setError("Enter an order number."); return; }
    try {
      const result = await request(`/orders/${encodeURIComponent(orderIdToTrack.trim())}`);
      setTrackedOrder(result); setPage("track");
    } catch (e2) { setError(e2.message); }
  }

  useEffect(() => {
    if (page !== "confirmation" && page !== "track") return;
    const id = order?.id || trackedOrder?.id;
    if (!id) return;
    const timer = setInterval(async () => {
      try {
        const latest = await request(`/orders/${id}`);
        setTrackedOrder(latest);
        if (order?.id === latest.id) setOrder(latest);
      } catch { /* keep last known order visible */ }
    }, 4000);
    return () => clearInterval(timer);
  }, [page, order?.id, trackedOrder?.id]);

  return <div className="app-shell">
    <Header cartCount={cartCount} onCart={() => setPage("cart")} />
    <main className="content">
      {error && <div className="banner error-banner"><span>⚠</span><span>{error}</span><button onClick={() => setError("")}>×</button></div>}
      {notice && <div className="banner success-banner">{notice}<button onClick={() => setNotice("")}>×</button></div>}

      {page === "menu" && <>
        <section className="hero">
          <div><span className="eyebrow">LESS WAITING. BETTER FLOW.</span><h1>Good food.<br/><em>No long queues.</em></h1><p>Order ahead, pay at checkout, and get your assigned counter.</p>
            <div className="hero-pills"><span>✓ Live menu</span><span>✓ Smart counter allocation</span><span>✓ Order tracking</span></div>
          </div>
          <div className="hero-qr"><QRCodeSVG value={`${window.location.origin}/?qr_scan=1`} size={126} level="M"/><small>SCAN TO CHECK IN & OPEN MENU</small></div>
        </section>
        <section className="section-head menu-heading"><div><span className="eyebrow">MADE FRESH ON CAMPUS</span><h2>Today's menu</h2></div><button className="outline-btn" onClick={loadMenu}>↻ Refresh</button></section>
        <div className="menu-tools"><input className="search-input" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search dosa, tea, snacks..." /><div className="category-row">{categories.map(c => <button key={c} className={`chip ${category === c ? "selected" : ""}`} onClick={() => setCategory(c)}>{c}</button>)}</div></div>
        {filteredMenu.length ? <div className="menu-grid">{filteredMenu.map(item => <article className="food-card" key={item.id}>
          <div className="food-emoji">{item.image_emoji || "🍽️"}</div><div className="food-info"><span className="food-category">{item.category}</span><h3>{item.name}</h3><p>{item.description || "Freshly prepared at the canteen."}</p><div className="food-bottom"><div><strong>{money(item.price)}</strong><small>{item.stock_quantity} available</small></div>{cart[String(item.id)] ? <div className="qty-control"><button onClick={() => changeQty(item.id,-1)}>−</button><b>{cart[String(item.id)]}</b><button onClick={() => changeQty(item.id,1)} disabled={cart[String(item.id)] >= item.stock_quantity}>+</button></div> : <button className="add-btn" onClick={() => changeQty(item.id,1)}>+ Add</button>}</div></div>
        </article>)}</div> : <div className="empty-state">No items match your search.</div>}
        {combos.length > 0 && <section className="combo-strip"><div><span className="eyebrow">SAVE ON YOUR FAVOURITES</span><h3>Combo offers</h3></div><div className="combo-options">{combos.map(c => <button key={c.id} className={`combo-card ${String(selectedCombo) === String(c.id) ? "selected" : ""}`} onClick={() => setSelectedCombo(String(selectedCombo) === String(c.id) ? "" : String(c.id))}><span>🎁</span><div><strong>{c.name}</strong><small>{c.description}</small></div><b>{money(c.price)}</b></button>)}</div><p className="muted tiny">Combo discounts apply when all included items are in your cart.</p></section>}
        <section className="track-strip"><div><div className="eyebrow">ALREADY ORDERED?</div><h3>Track your order</h3><p className="muted">Enter the order number from your receipt.</p></div><form onSubmit={trackOrder} className="track-form"><input value={orderIdToTrack} onChange={e => setOrderIdToTrack(e.target.value)} placeholder="Order number e.g. 12"/><button className="dark-btn">Track</button></form></section>
      </>}

      {page === "cart" && <section className="checkout-layout"><div><button className="back-link" onClick={() => setPage("menu")}>← Back to menu</button><span className="eyebrow">YOUR SELECTION</span><h1>Your cart</h1>
        {cartLines.length ? <div className="cart-list">{cartLines.map(({item,quantity}) => <article className="cart-line" key={item.id}><div className="cart-emoji">{item.image_emoji || "🍽️"}</div><div className="cart-line-info"><strong>{item.name}</strong><small>{money(item.price)} each</small></div><div className="qty-control"><button onClick={() => changeQty(item.id,-1)}>−</button><b>{quantity}</b><button onClick={() => changeQty(item.id,1)} disabled={quantity >= item.stock_quantity}>+</button></div><strong>{money(Number(item.price)*quantity)}</strong><button className="remove-btn" onClick={() => changeQty(item.id,-quantity)} aria-label="Remove item">×</button></article>)}</div> : <div className="empty-state">Your cart is empty. <button className="text-btn" onClick={() => setPage("menu")}>Browse menu</button></div>}
      </div><aside className="summary-card"><h3>Order summary</h3><div className="summary-row"><span>Subtotal ({cartCount} items)</span><span>{money(subtotal)}</span></div>{comboDiscount > 0 && <div className="summary-row discount"><span>Combo discount</span><span>−{money(comboDiscount)}</span></div>}<div className="summary-total"><span>Total</span><strong>{money(total)}</strong></div><button className="primary-btn full" disabled={!cartLines.length} onClick={() => setPage("checkout")}>Continue to checkout →</button><p className="muted tiny">Counter assignment happens after you place the order.</p></aside></section>}

      {page === "checkout" && <section className="checkout-layout"><div><button className="back-link" onClick={() => setPage("cart")}>← Back to cart</button><span className="eyebrow">ALMOST THERE</span><h1>Checkout</h1><form className="checkout-form" onSubmit={placeOrder}><label>Your name<input value={name} onChange={e => setName(e.target.value)} required maxLength={80} placeholder="Enter your name"/></label><fieldset><legend>Payment method</legend><label className={`payment-choice ${paymentMethod === "cash" ? "chosen" : ""}`}><input type="radio" value="cash" checked={paymentMethod === "cash"} onChange={() => setPaymentMethod("cash")}/><span>💵</span><span><strong>Cash at canteen</strong><small>Demo records payment as cash selected</small></span></label><label className={`payment-choice ${paymentMethod === "online" ? "chosen" : ""}`}><input type="radio" value="online" checked={paymentMethod === "online"} onChange={() => setPaymentMethod("online")}/><span>📱</span><span><strong>Online payment</strong><small>Simulated only — no real charge</small></span></label></fieldset><button className="primary-btn full" disabled={busy || !cartLines.length}>{busy ? "Placing order..." : `Place order · ${money(total)}`}</button></form></div><aside className="summary-card"><h3>Order summary</h3>{cartLines.map(({item,quantity}) => <div className="summary-row" key={item.id}><span>{item.name} × {quantity}</span><span>{money(Number(item.price)*quantity)}</span></div>)}{comboDiscount > 0 && <div className="summary-row discount"><span>Combo discount</span><span>−{money(comboDiscount)}</span></div>}<div className="summary-total"><span>Total</span><strong>{money(total)}</strong></div></aside></section>}

      {(page === "confirmation" || page === "track") && (trackedOrder || order) && (() => {
        const current = trackedOrder || order;
        return <section className="confirmation">
          <div className="confirmation-icon">{current.status === "completed" ? "✓" : "🍽️"}</div><span className="eyebrow">{page === "confirmation" ? "ORDER CONFIRMED" : "LIVE ORDER STATUS"}</span><h1>{current.status === "completed" ? "Enjoy your meal!" : current.status === "ready" ? "Your order is ready" : "You're all set."}</h1><p className="muted">Order #{current.id} · placed {dateTime(current.created_at)}</p>
          <div className="assignment-card"><div><span className="eyebrow">YOUR PICKUP COUNTER</span><div className="big-counter">Counter {current.counter_number}</div><p>Head to Counter {current.counter_number} when your order is ready.</p></div><div className="status-badge">{prettyStatus(current.status)}</div></div>
          <div className="order-detail-card"><div className="section-head"><h3>Order details</h3><strong>{money(current.total_amount)}</strong></div>{(current.items || []).map((line,i) => <div className="summary-row" key={`${line.item_id}-${i}`}><span>{line.name} × {line.quantity}</span><span>{money(line.line_total)}</span></div>)}<div className="summary-row"><span>Payment</span><strong>{current.payment_status === "paid" ? `Paid (${current.payment_method})` : "Pending"}</strong></div><div className="summary-row"><span>Estimated wait</span><strong>~{current.estimated_wait_minutes || 0} min</strong></div></div>
          <div className="timeline">{["queued","preparing","ready","completed"].map((s,i) => <div key={s} className={`timeline-step ${["queued","preparing","ready","completed"].indexOf(current.status) >= i ? "done" : ""}`}><span>{i+1}</span><small>{prettyStatus(s)}</small></div>)}</div>
          <div className="confirmation-actions"><button className="primary-btn" onClick={() => {setOrder(null);setTrackedOrder(null);setPage("menu");setNotice("");loadMenu();}}>Order more</button><button className="outline-btn" onClick={() => {setOrderIdToTrack(String(current.id));setPage("track");}}>Keep tracking</button></div>
          <p className="muted tiny">Status refreshes automatically every 4 seconds. Online payment in this prototype is simulated.</p>
        </section>;
      })()}
    </main>
    <div className="floating-cart" hidden={page !== "menu" || cartCount === 0}><button className="primary-btn" onClick={() => setPage("cart")}>🛒 View cart <span>{cartCount} items</span><b>{money(subtotal)}</b></button></div>
    <footer className="footer">Smart Canteen Queue & Counter Planning · SQLite prototype</footer>
  </div>;
}

function ManagerPage() {
  const [tab, setTab] = useState("orders");
  const [orders, setOrders] = useState([]);
  const [items, setItems] = useState([]);
  const [counters, setCounters] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [newItem, setNewItem] = useState({name:"",category:"Snacks",price:"",stock_quantity:"",prep_minutes:"3",image_emoji:"🍽️",description:""});
  const [stockEdits, setStockEdits] = useState({});
  const [lastAlertIds, setLastAlertIds] = useState([]);

  const refresh = useCallback(async () => {
    try {
      const [o, i, c, a, an] = await Promise.all([
        request("/orders?limit=100"), request("/items"), request("/counters"),
        request("/alerts"), request("/analytics")
      ]);
      setOrders(Array.isArray(o) ? o : []);
      setItems(Array.isArray(i) ? i : []);
      setCounters(Array.isArray(c) ? c : []);
      setAlerts(Array.isArray(a) ? a : []);
      setAnalytics(an);
      setError("");
    } catch (e) { setError(`Backend request failed: ${e.message}. Check that FastAPI is running on port 8000.`); }
  }, []);

  useEffect(() => { refresh(); const timer = setInterval(refresh, 3500); return () => clearInterval(timer); }, [refresh]);

  useEffect(() => {
    const unread = alerts.filter(a => !a.read && !lastAlertIds.includes(a.id));
    if (unread.length) {
      unread.forEach(a => { if (a.type === "qr_counter" || a.type === "counter" || a.type === "auto_counter") window.alert(`WORKER ALERT: ${a.message}`); });
      setLastAlertIds(prev => [...prev, ...unread.map(a => a.id)]);
    }
  }, [alerts, lastAlertIds]);

  async function act(fn, success) {
    setBusy(true); setError(""); setMessage("");
    try { await fn(); await refresh(); if (success) setMessage(success); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }

  async function updateOrder(id, status) {
    await act(() => request(`/orders/${id}/status`, {method:"PATCH",body:JSON.stringify({status})}), `Order #${id} updated.`);
  }
  async function toggleCounter(c) {
    await act(() => request(`/counters/${c.number}`, {method:"PATCH",body:JSON.stringify({is_active:!c.is_active})}), `Counter ${c.number} ${c.is_active ? "closed" : "opened"}.`);
  }
  async function saveStock(item) {
    const value = Number(stockEdits[item.id] ?? item.stock_quantity);
    await act(() => request(`/items/${item.id}/stock`, {method:"PATCH",body:JSON.stringify({stock_quantity:value})}), `Stock updated for ${item.name}.`);
  }
  async function addItem(e) {
    e.preventDefault();
    await act(async () => {
      await request("/items", {method:"POST",body:JSON.stringify({...newItem,price:Number(newItem.price),stock_quantity:Number(newItem.stock_quantity),prep_minutes:Number(newItem.prep_minutes),is_available:true})});
      setNewItem({name:"",category:"Snacks",price:"",stock_quantity:"",prep_minutes:"3",image_emoji:"🍽️",description:""});
    }, "Menu item added.");
  }
  async function acknowledge(alertId) {
    await act(() => request(`/alerts/${alertId}/read`, {method:"POST"}), "Alert acknowledged.");
  }
  const activeCounters = counters.filter(c => c.is_active).length;
  const waitingOrders = orders.filter(o => ["queued","preparing","ready"].includes(o.status)).length;

  return <div className="app-shell">
    <Header admin />
    <main className="content admin-content">
      <section className="admin-title"><div><span className="eyebrow">CANTEEN OPERATIONS</span><h1>Manager dashboard</h1><p className="muted">Orders, inventory, counter allocation and sales in one place.</p></div><button className="outline-btn" onClick={refresh}>↻ Refresh</button></section>
      {error && <div className="banner error-banner">{error}<button onClick={() => setError("")}>×</button></div>}
      {message && <div className="banner success-banner">{message}<button onClick={() => setMessage("")}>×</button></div>}
      <section className="stats-grid">
        <Stat label="Orders today" value={analytics?.orders_today ?? orders.length} icon="🧾"/>
        <Stat label="Waiting orders" value={waitingOrders} icon="⏱️"/>
        <Stat label="Active counters" value={`${activeCounters} / 4`} icon="🏪"/>
        <Stat label="Total QR scans" value={analytics?.total_qr_scans ?? 0} icon="📱"/>
        <Stat label="Paid sales today" value={money(analytics?.paid_sales_today)} icon="₹"/>
      </section>
      <nav className="admin-tabs">{[["orders","Orders"],["inventory","Menu & inventory"],["counters","Counters"],["analytics","Analytics"],["alerts","Alerts & QR rules"]].map(([id,label]) => <button key={id} className={tab===id?"active":""} onClick={() => setTab(id)}>{label}{id==="alerts" && alerts.filter(a=>!a.read).length>0 ? ` (${alerts.filter(a=>!a.read).length})` : ""}</button>)}</nav>

      {tab === "orders" && <section className="panel admin-panel"><div className="section-head"><div><span className="eyebrow">LIVE ORDER QUEUE</span><h2>Incoming orders</h2></div><span className="muted tiny">Auto-refreshes every 3.5 seconds</span></div>
        {orders.length === 0 ? <Empty text="No orders yet. Place a test order from the student menu."/> : <div className="table-scroll"><table><thead><tr><th>Order</th><th>Customer/items</th><th>Counter</th><th>Total</th><th>Payment</th><th>Status</th><th>Update</th></tr></thead><tbody>{orders.map(o=><tr key={o.id}><td><strong>#{o.id}</strong><small>{new Date(o.created_at).toLocaleTimeString()}</small></td><td><strong>{o.customer_name}</strong><small>{(o.items||[]).map(i=>`${i.name} ×${i.quantity}`).join(", ")}</small></td><td>Counter {o.counter_number}</td><td>{money(o.total_amount)}</td><td><span className={`status-pill ${o.payment_status}`}>{o.payment_status}</span></td><td><span className={`status-pill ${o.status}`}>{prettyStatus(o.status)}</span></td><td><select value={o.status} disabled={busy || ["completed","cancelled"].includes(o.status)} onChange={e=>updateOrder(o.id,e.target.value)}><option value="queued">Queued</option><option value="preparing">Preparing</option><option value="ready">Ready</option><option value="completed">Completed</option><option value="cancelled">Cancel</option></select></td></tr>)}</tbody></table></div>}
      </section>}

      {tab === "inventory" && <div className="admin-two-col"><section className="panel admin-panel"><span className="eyebrow">CURRENT MENU</span><h2>Inventory levels</h2><div className="inventory-list">{items.map(item=><div className="inventory-row" key={item.id}><span className="inventory-emoji">{item.image_emoji}</span><div className="inventory-info"><strong>{item.name}</strong><small>{item.category} · {money(item.price)}</small></div><label className={item.stock_quantity<=5?"stock-low":""}>Stock<input type="number" min="0" value={stockEdits[item.id] ?? item.stock_quantity} onChange={e=>setStockEdits(s=>({...s,[item.id]:e.target.value}))}/></label><button className="small-btn" disabled={busy} onClick={()=>saveStock(item)}>Save</button></div>)}</div></section>
        <section className="panel admin-panel"><span className="eyebrow">ADD PRODUCT</span><h2>Add menu item</h2><form className="add-item-form" onSubmit={addItem}><label>Item name<input required value={newItem.name} onChange={e=>setNewItem(s=>({...s,name:e.target.value}))} placeholder="e.g. Pongal"/></label><div className="form-row"><label>Category<select value={newItem.category} onChange={e=>setNewItem(s=>({...s,category:e.target.value}))}><option>Breakfast</option><option>Snacks</option><option>Drinks</option><option>Lunch</option><option>Desserts</option></select></label><label>Emoji<input value={newItem.image_emoji} onChange={e=>setNewItem(s=>({...s,image_emoji:e.target.value}))}/></label></div><div className="form-row"><label>Price (₹)<input required type="number" min="0" step="0.5" value={newItem.price} onChange={e=>setNewItem(s=>({...s,price:e.target.value}))}/></label><label>Opening stock<input required type="number" min="0" value={newItem.stock_quantity} onChange={e=>setNewItem(s=>({...s,stock_quantity:e.target.value}))}/></label></div><label>Prep time (minutes)<input type="number" min="1" value={newItem.prep_minutes} onChange={e=>setNewItem(s=>({...s,prep_minutes:e.target.value}))}/></label><label>Description<input value={newItem.description} onChange={e=>setNewItem(s=>({...s,description:e.target.value}))}/></label><button className="primary-btn full" disabled={busy}>Add item to menu</button></form></section></div>}

      {tab === "counters" && <section className="panel admin-panel"><div className="section-head"><div><span className="eyebrow">RESOURCE ALLOCATION</span><h2>Counter status</h2><p className="muted">Counter 1 starts open. Open additional counters during peak demand.</p></div></div><div className="counter-grid">{counters.map(c=><article className={`counter-card ${c.is_active?"active":"inactive"}`} key={c.number}><div className="counter-top"><span className="counter-icon">C{c.number}</span><span className={`status-pill ${c.is_active?"paid":"cancelled"}`}>{c.is_active?"ACTIVE":"CLOSED"}</span></div><h3>Counter {c.number}</h3><div className="counter-load">{c.waiting}<small> waiting</small></div><div className="progress"><span style={{width:`${Math.min(100,c.waiting*10)}%`}}/></div><p className="muted tiny">{c.queue_length} in queue · ~{c.estimated_wait_minutes} min estimated</p><button className={c.is_active?"outline-btn":"primary-btn"} disabled={busy || (c.is_active && c.queue_length>0) || (c.is_active && activeCounters<=1)} onClick={()=>toggleCounter(c)}>{c.is_active?"Close counter":"Open counter"}</button>{c.is_active && <p className="muted tiny">{c.completed_today} completed today</p>}</article>)}</div></section>}

      {tab === "analytics" && <div className="analytics-grid"><section className="panel admin-panel"><span className="eyebrow">PERFORMANCE</span><h2>Today's summary</h2><div className="analytics-rows"><div><span>Orders placed</span><strong>{analytics?.orders_today ?? 0}</strong></div><div><span>Orders completed</span><strong>{analytics?.completed_today ?? 0}</strong></div><div><span>Paid sales</span><strong>{money(analytics?.paid_sales_today)}</strong></div><div><span>Total QR scans</span><strong>{analytics?.total_qr_scans ?? 0}</strong></div><div><span>QR scans today</span><strong>{analytics?.qr_scans_today ?? 0}</strong></div><div><span>Students waiting</span><strong>{analytics?.total_qr_waiting ?? 0}</strong></div></div></section><section className="panel admin-panel"><span className="eyebrow">POPULAR ITEMS</span><h2>Top sellers</h2>{(analytics?.top_items||[]).length ? analytics.top_items.map((item,i)=><div className="top-item" key={item.name}><span className="rank">{i+1}</span><strong>{item.name}</strong><span>{item.quantity} sold</span></div>) : <Empty text="Sales appear here after orders are placed."/>}</section><section className="panel admin-panel"><span className="eyebrow">RESTOCK PLANNING</span><h2>Stock recommendations</h2><div className="table-scroll"><table><thead><tr><th>Item</th><th>Stock</th><th>Sold today</th><th>Suggested restock</th></tr></thead><tbody>{(analytics?.stock_recommendations||[]).map(s=><tr key={s.item_id}><td>{s.name}{s.low_stock&&<small className="stock-warning">Low stock</small>}</td><td>{s.stock_quantity}</td><td>{s.sold_today}</td><td>{s.recommended_restock}</td></tr>)}</tbody></table></div></section><section className="panel admin-panel qr-insights"><span className="eyebrow">LIVE QUEUE BALANCE</span><h2>QR counter utilization</h2><p className="muted">{analytics?.queue_insight || "Queue status will appear here."}</p><div className="counter-grid">{(analytics?.counters || []).map(c=><article className={`counter-card ${c.is_active ? "active" : "inactive"}`} key={c.counter_number}><div className="counter-top"><strong>{c.name}</strong><span className={`status-pill ${c.is_active ? "paid" : "cancelled"}`}>{c.is_active ? "ACTIVE" : "CLOSED"}</span></div><div className="counter-load">{c.qr_waiting}<small> / {c.is_active ? c.qr_capacity : 10} waiting</small></div><div className="progress"><span style={{width:`${c.qr_utilization_percent}%`}}/></div><p className="muted tiny">{c.is_active ? `${c.qr_available_slots} QR places available` : "Opens automatically as active counters fill"}{c.is_active && c.qr_waiting === 0 ? " · idle" : ""}</p></article>)}</div><p className="muted tiny">Each open counter serves up to 10 waiting QR students. Scans go to the shortest active queue; the next counter opens automatically when current capacity fills.</p></section></div>}

      {tab === "alerts" && <div className="admin-two-col"><section className="panel admin-panel"><span className="eyebrow">STAFF MESSAGES</span><h2>Worker alerts</h2>{alerts.length===0?<Empty text="No alerts yet."/>:alerts.map(a=><div className={`alert-row ${a.read?"read":""}`} key={a.id}><span className="alert-dot">!</span><div className="alert-content"><strong>{a.message}</strong><small>{dateTime(a.created_at)}</small></div>{!a.read&&<button className="small-btn" onClick={()=>acknowledge(a.id)} disabled={busy}>Acknowledge</button>}</div>)}</section><section className="panel admin-panel"><span className="eyebrow">AUTOMATIC QR QUEUE</span><h2>Counter opening rules</h2><div className="analytics-rows"><div><span>Waiting capacity per counter</span><strong>10 students</strong></div><div><span>Counter assignment</span><strong>Shortest active queue</strong></div><div><span>Peak mode required</span><strong>No</strong></div></div><p className="muted">When the waiting QR capacity of all open counters is full, the next configured counter opens automatically. Completed QR customers free up queue capacity.</p></section></div>}
    </main>
    <footer className="footer">Manager tools · SQLite database · Local prototype</footer>
  </div>;
}

function Stat({label,value,icon}) { return <article className="stat-card"><span className="stat-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong></div></article>; }
function Empty({text}) { return <div className="empty-state">{text}</div>; }

createRoot(document.getElementById("root")).render(<AppErrorBoundary><App /></AppErrorBoundary>);
