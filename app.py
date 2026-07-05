#!/usr/bin/env python3
"""
app.py — ORDR · Plateforme Click & Collect
"""

from __future__ import annotations
import os, json, time, uuid, functools, smtplib
from email.mime.text import MIMEText
from flask import (Flask, request, jsonify, render_template,
                   redirect, send_from_directory, session)

try:
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
except ImportError:
    stripe = None

from menu_extractor import extract_from_image, extract_from_google_places
import db

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-this-secret-in-production")

BASE_URL  = os.environ.get("BASE_URL", "http://localhost:8080")
ADMIN_PWD = os.environ.get("ADMIN_PASSWORD", "ordr2024")

PLATFORM_FEE_PERCENT = 0.10
PLATFORM_FEE_FIXED   = 0.30

db.init_db()


# ── Helpers ────────────────────────────────────────────────────────────────────

def platform_fee(total_eur: float) -> int:
    return int(round((total_eur * PLATFORM_FEE_PERCENT + PLATFORM_FEE_FIXED) * 100))


# ── Notifications email ────────────────────────────────────────────────────────

def try_notify_order(shop_id: str, order: dict):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASS")
    dest = os.environ.get("NOTIFY_EMAIL") or user
    if not all([host, user, pwd, dest]):
        return
    try:
        shop      = db.get_shop(shop_id)
        shop_name = (shop or {}).get("nom_commerce", shop_id)
        items_txt = "".join(
            f"  • {it.get('qty',1)}× {it.get('name','?')} — {it.get('price',0):.2f}€\n"
            for it in order.get("items", [])
        )
        body = (
            f"Nouvelle commande reçue !\n\n"
            f"Commerce : {shop_name}\n"
            f"Client    : {order.get('customer_name','—')}\n"
            f"Téléphone : {order.get('customer_phone','—')}\n"
            f"Créneau   : {order.get('slot','—')}\n\n"
            f"Articles :\n{items_txt or '  (détail non disponible)'}\n"
            f"Total     : {order.get('total',0):.2f} €\n\n"
            f"→ Voir les commandes : {BASE_URL}/admin/shop/{shop_id}"
        )
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"Nouvelle commande — {shop_name}"
        msg["From"]    = user
        msg["To"]      = dest
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
    except Exception:
        pass


# ── Admin auth ─────────────────────────────────────────────────────────────────

def admin_required(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("admin_ok"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return wrapped

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PWD:
            session["admin_ok"] = True
            return redirect("/admin")
        error = "Mot de passe incorrect."
    return render_template("admin_login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


# ── Routes Client ──────────────────────────────────────────────────────────────

@app.route("/shop/<shop_id>")
def storefront(shop_id):
    shop = db.get_shop(shop_id)
    if not shop:
        return render_template("404.html"), 404
    return render_template(
        "storefront.html", shop=shop, shop_id=shop_id,
        stripe_pk=os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    )

@app.route("/api/checkout", methods=["POST"])
def create_checkout():
    data           = request.get_json()
    shop_id        = data.get("shop_id")
    items          = data.get("items", [])
    slot           = data.get("slot", "")
    customer_name  = data.get("customer_name", "")
    customer_phone = data.get("customer_phone", "")

    if not items:
        return jsonify({"error": "Panier vide"}), 400

    shop = db.get_shop(shop_id)
    if not shop:
        return jsonify({"error": "Commerce inconnu"}), 404

    # Mode paiement sur place : si Stripe pas configuré, on enregistre la commande directement
    stripe_ready = (stripe and stripe.api_key
                    and shop.get("stripe_account_id")
                    and shop.get("stripe_onboarding_complete"))

    if not stripe_ready:
        total = sum(it.get("price", 0) * it.get("qty", 1) for it in items)
        order = {
            "stripe_session_id": None,
            "items":             items,
            "total":             total,
            "slot":              slot,
            "customer_name":     customer_name,
            "customer_phone":    customer_phone,
            "status":            "à régler sur place",
        }
        db.create_order(shop_id, order)
        try_notify_order(shop_id, order)
        return jsonify({"cash": True, "shop_id": shop_id})

    line_items, total = [], 0.0
    for it in items:
        price = it.get("price", 0)
        if price <= 0:
            continue
        qty    = it.get("qty", 1)
        total += price * qty
        line_items.append({
            "price_data": {
                "currency": "eur",
                "product_data": {"name": it["name"]},
                "unit_amount": int(round(price * 100)),
            },
            "quantity": qty,
        })

    if not line_items:
        return jsonify({"error": "Aucun article avec prix valide"}), 400

    try:
        sess = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=line_items,
            mode="payment",
            payment_intent_data={
                "application_fee_amount": platform_fee(total),
                "transfer_data": {"destination": shop["stripe_account_id"]},
            },
            success_url=(
                f"{BASE_URL}/order-confirmed"
                f"?session_id={{CHECKOUT_SESSION_ID}}&shop={shop_id}"
            ),
            cancel_url=f"{BASE_URL}/shop/{shop_id}",
            metadata={
                "shop_id":        shop_id,
                "creneau":        slot,
                "customer_name":  customer_name,
                "customer_phone": customer_phone,
            },
        )
        return jsonify({"checkout_url": sess.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/order-confirmed")
def order_confirmed():
    stripe_session_id = request.args.get("session_id")
    shop_id           = request.args.get("shop")
    is_cash           = request.args.get("cash") == "1"
    shop              = db.get_shop(shop_id) if shop_id else None
    order             = {}

    # Mode paiement en ligne : récupère la commande depuis Stripe
    if not is_cash and stripe and stripe_session_id and not db.order_exists(stripe_session_id):
        try:
            sess  = stripe.checkout.Session.retrieve(
                stripe_session_id, expand=["line_items"]
            )
            items = [
                {"name": li.description, "qty": li.quantity, "price": li.amount_total / 100}
                for li in (sess.line_items.data if sess.line_items else [])
            ]
            order = {
                "stripe_session_id": stripe_session_id,
                "items":             items,
                "total":             (sess.amount_total or 0) / 100,
                "slot":              sess.metadata.get("creneau", ""),
                "customer_name":     sess.metadata.get("customer_name", ""),
                "customer_phone":    sess.metadata.get("customer_phone", ""),
            }
            db.create_order(shop_id, order)
            try_notify_order(shop_id, order)
        except Exception:
            pass

    return render_template("confirmed.html", shop=shop, order=order)


# ── Stripe Webhook ─────────────────────────────────────────────────────────────

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    secret  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not secret:
        return "", 400

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:
        return "", 400

    if event["type"] == "checkout.session.completed":
        sess    = event["data"]["object"]
        shop_id = sess["metadata"].get("shop_id")
        if shop_id and not db.order_exists(sess["id"]):
            try:
                full  = stripe.checkout.Session.retrieve(sess["id"], expand=["line_items"])
                items = [
                    {"name": li.description, "qty": li.quantity, "price": li.amount_total / 100}
                    for li in (full.line_items.data if full.line_items else [])
                ]
            except Exception:
                items = []
            order = {
                "stripe_session_id": sess["id"],
                "items":             items,
                "total":             (sess.get("amount_total") or 0) / 100,
                "slot":              sess["metadata"].get("creneau", ""),
                "customer_name":     sess["metadata"].get("customer_name", ""),
                "customer_phone":    sess["metadata"].get("customer_phone", ""),
            }
            db.create_order(shop_id, order)
            try_notify_order(shop_id, order)

    return "", 200


# ── Routes Admin ───────────────────────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_home():
    shops = db.list_shops()
    stats = {
        "total_shops":  len(shops),
        "total_orders": sum(len(db.list_orders(s["id"])) for s in shops),
    }
    return render_template("admin.html", shops=shops, stats=stats)

@app.route("/admin/shop/<shop_id>")
@admin_required
def admin_shop(shop_id):
    shop = db.get_shop(shop_id)
    if not shop:
        return render_template("404.html"), 404
    orders   = db.list_orders(shop_id)
    merchant = db.get_merchant_by_shop(shop_id)
    shop["has_merchant"] = merchant is not None
    return render_template(
        "admin_shop.html",
        shop=shop, shop_id=shop_id, orders=orders,
        public_url=f"{BASE_URL}/shop/{shop_id}",
        base_url=BASE_URL,
    )

@app.route("/admin/new")
@admin_required
def admin_new_shop():
    return render_template("admin_new.html")

@app.route("/admin/shop/<shop_id>/connect-stripe", methods=["POST"])
@admin_required
def connect_stripe(shop_id):
    if not stripe or not stripe.api_key:
        return jsonify({"error": "Stripe non configuré"}), 500
    shop = db.get_shop(shop_id)
    if not shop:
        return jsonify({"error": "Commerce inconnu"}), 404
    try:
        account_id = shop.get("stripe_account_id")
        if not account_id:
            account    = stripe.Account.create(
                type="express", country="FR",
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers":     {"requested": True},
                },
            )
            account_id = account.id
            shop["stripe_account_id"]         = account_id
            shop["stripe_onboarding_complete"] = False
            db.update_shop(shop_id, shop)
        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=f"{BASE_URL}/admin/shop/{shop_id}",
            return_url=f"{BASE_URL}/admin/shop/{shop_id}?stripe_return=1",
            type="account_onboarding",
        )
        return jsonify({"onboarding_url": link.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/shop/<shop_id>/stripe-status")
@admin_required
def stripe_status(shop_id):
    shop = db.get_shop(shop_id)
    if not shop or not shop.get("stripe_account_id"):
        return jsonify({"connected": False})
    if not stripe or not stripe.api_key:
        return jsonify({"connected": False, "error": "Stripe non configuré"})
    try:
        account = stripe.Account.retrieve(shop["stripe_account_id"])
        ready   = account.charges_enabled and account.payouts_enabled
        if ready != shop.get("stripe_onboarding_complete"):
            shop["stripe_onboarding_complete"] = ready
            db.update_shop(shop_id, shop)
        return jsonify({
            "connected":       True,
            "charges_enabled": account.charges_enabled,
            "payouts_enabled": account.payouts_enabled,
            "ready":           ready,
        })
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})

@app.route("/api/extract-menu", methods=["POST"])
@admin_required
def api_extract_menu():
    google_query = request.form.get("google_query", "").strip()
    if google_query:
        result = extract_from_google_places(google_query)
        if result:
            sid = db.create_shop(result)
            return jsonify({"success": True, "shop_id": sid,
                            "source": "google", "catalogue": result})

    photo = request.files.get("photo")
    if not photo:
        return jsonify({"error": "Aucune photo ni recherche fournie"}), 400

    ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
    if ext not in {"jpg", "jpeg", "png", "webp"}:
        return jsonify({"error": f"Format non supporté (.{ext})"}), 400

    raw = photo.read()
    if len(raw) < 5000:
        return jsonify({"error": "Fichier trop petit ou corrompu"}), 400

    try:
        result = extract_from_image(raw, photo.filename)
    except Exception as e:
        return jsonify({"error": f"Extraction échouée : {e}"}), 500

    if "erreur" in result:
        return jsonify({"error": result.get("detail", result["erreur"])}), 400

    sid = db.create_shop(result)
    return jsonify({"success": True, "shop_id": sid,
                    "source": "photo", "catalogue": result})

@app.route("/api/shop/<shop_id>/update-menu", methods=["POST"])
@admin_required
def api_update_menu(shop_id):
    shop = db.get_shop(shop_id)
    if not shop:
        return jsonify({"error": "Commerce inconnu"}), 404
    photo = request.files.get("photo")
    if not photo:
        return jsonify({"error": "Aucune photo fournie"}), 400
    raw = photo.read()
    try:
        result = extract_from_image(raw, photo.filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if "erreur" in result:
        return jsonify({"error": result.get("detail", result["erreur"])}), 400
    if shop.get("nom_commerce") and not result.get("nom_commerce"):
        result["nom_commerce"] = shop["nom_commerce"]
    result["stripe_account_id"]         = shop.get("stripe_account_id")
    result["stripe_onboarding_complete"] = shop.get("stripe_onboarding_complete")
    db.update_shop(shop_id, result)
    return jsonify({"success": True, "catalogue": result})

@app.route("/api/shop/<shop_id>/edit", methods=["POST"])
@admin_required
def api_edit_product(shop_id):
    shop = db.get_shop(shop_id)
    if not shop:
        return jsonify({"error": "Commerce inconnu"}), 404
    data     = request.get_json()
    action   = data.get("action")
    cat_idx  = data.get("cat_idx")
    prod_idx = data.get("prod_idx")
    try:
        cat = shop["categories"][cat_idx]
        if action == "delete":
            cat["produits"].pop(prod_idx)
        elif action == "update":
            prod = cat["produits"][prod_idx]
            for field in ("nom", "prix", "description"):
                if field in data:
                    prod[field] = data[field]
        elif action == "add":
            cat["produits"].append({
                "nom":         data.get("nom", "Nouveau produit"),
                "prix":        data.get("prix"),
                "description": data.get("description"),
            })
    except (IndexError, KeyError, TypeError):
        return jsonify({"error": "Produit/catégorie introuvable"}), 404
    shop["total_produits"] = sum(
        len(c.get("produits", [])) for c in shop.get("categories", [])
    )
    db.update_shop(shop_id, shop)
    return jsonify({"success": True})

@app.route("/api/shop/<shop_id>/rename", methods=["POST"])
@admin_required
def api_rename_shop(shop_id):
    shop = db.get_shop(shop_id)
    if not shop:
        return jsonify({"error": "Commerce inconnu"}), 404
    name = (request.get_json() or {}).get("nom", "").strip()
    if not name:
        return jsonify({"error": "Nom vide"}), 400
    shop["nom_commerce"] = name
    db.update_shop(shop_id, shop)
    return jsonify({"success": True})

@app.route("/api/shop/create-manual", methods=["POST"])
@admin_required
def api_create_manual():
    data = request.get_json() or {}
    name = data.get("nom", "").strip()
    addr = data.get("adresse", "").strip()
    if not name:
        return jsonify({"error": "Nom requis"}), 400
    sid = db.create_shop({
        "nom_commerce":   name,
        "adresse":        addr,
        "categories":     [],
        "total_produits": 0,
    })
    return jsonify({"success": True, "shop_id": sid})

@app.route("/api/shop/<shop_id>/horaires", methods=["POST"])
@admin_required
def api_set_horaires(shop_id):
    shop = db.get_shop(shop_id)
    if not shop:
        return jsonify({"error": "Commerce inconnu"}), 404
    shop["horaires"] = request.get_json() or {}
    db.update_shop(shop_id, shop)
    return jsonify({"success": True})

@app.route("/api/shop/<shop_id>/merchant-password", methods=["POST"])
@admin_required
def api_set_merchant_password(shop_id):
    pwd = (request.get_json() or {}).get("password", "").strip()
    if len(pwd) < 4:
        return jsonify({"error": "Mot de passe trop court (4 caractères min)"}), 400
    db.create_merchant(shop_id, pwd)
    return jsonify({"success": True})

@app.route("/api/shop/<shop_id>/delete", methods=["POST"])
@admin_required
def api_delete_shop(shop_id):
    db.delete_shop(shop_id)
    return jsonify({"success": True})


# ── Espace commerçant ──────────────────────────────────────────────────────────

def merchant_required(f):
    @functools.wraps(f)
    def wrapped(shop_id, *args, **kwargs):
        if not session.get(f"merchant_{shop_id}"):
            return redirect(f"/m/{shop_id}")
        return f(shop_id, *args, **kwargs)
    return wrapped

@app.route("/m/<shop_id>", methods=["GET", "POST"])
def merchant_login(shop_id):
    shop = db.get_shop(shop_id)
    if not shop:
        return render_template("404.html"), 404
    merchant = db.get_merchant_by_shop(shop_id)
    if not merchant:
        return "Accès commerçant non configuré. Contactez ORDR.", 403
    error = None
    if request.method == "POST":
        if db.verify_merchant(shop_id, request.form.get("password", "")):
            session[f"merchant_{shop_id}"] = True
            return redirect(f"/m/{shop_id}/dashboard")
        error = "Mot de passe incorrect."
    return render_template("merchant_login.html", shop=shop, error=error)

@app.route("/m/<shop_id>/dashboard")
@merchant_required
def merchant_dashboard(shop_id):
    shop   = db.get_shop(shop_id)
    orders = db.list_orders(shop_id)
    return render_template(
        "merchant_dashboard.html",
        shop=shop, shop_id=shop_id, orders=orders,
        public_url=f"{BASE_URL}/shop/{shop_id}",
        now=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

@app.route("/m/<shop_id>/poll")
@merchant_required
def merchant_poll(shop_id):
    since = request.args.get("since", "")
    now   = time.strftime("%Y-%m-%d %H:%M:%S")
    count = db.count_new_orders(shop_id, since) if since else 0
    return jsonify({"new_count": count, "now": now})

@app.route("/m/<shop_id>/logout")
def merchant_logout(shop_id):
    session.pop(f"merchant_{shop_id}", None)
    return redirect(f"/m/{shop_id}")


# ── Static ─────────────────────────────────────────────────────────────────────

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)


# ── Launch ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  ORDR v2 — Click & Collect")
    print(f"  Storefront : {BASE_URL}/shop/<id>")
    print(f"  Admin      : {BASE_URL}/admin")
    print(f"  Webhook    : {BASE_URL}/webhook/stripe")
    print(f"  Base SQLite: {db.DB_PATH}\n")
    app.run(debug=True, port=port, host="0.0.0.0")
