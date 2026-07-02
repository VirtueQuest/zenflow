"""
ZenFlow — Phase 3: Stripe Payment Integration
──────────────────────────────────────────────
· Create PaymentIntents for token purchases
· Webhook handler for async payment confirmation
· Idempotency keys to prevent double-charging
· Support for SGD (Singapore Dollar)
· Test mode via STRIPE_TEST_KEY env var
"""

import os, logging, hashlib
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("zenflow.payments")

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PAYMENT_CURRENCY      = os.getenv("PAYMENT_CURRENCY", "sgd")   # or "usd"
STRIPE_ENABLED        = bool(STRIPE_SECRET_KEY)

# Token packages (tokens, price_usd, price_sgd)
PACKAGES = {
    "7":  {"tokens": 7,  "price_usd": 9.99,  "price_sgd": 1350,  "label": "7 Tokens — 7 Days Ad"},
    "30": {"tokens": 30, "price_usd": 29.99, "price_sgd": 4050,  "label": "30 Tokens — 30 Days Ad"},
    "90": {"tokens": 90, "price_usd": 59.99, "price_sgd": 8100,  "label": "90 Tokens — 90 Days Ad"},
}
# Stripe amounts are in smallest currency unit (cents / cents SGD)

def _get_stripe():
    if not STRIPE_ENABLED:
        raise ValueError("Stripe is not configured. Set STRIPE_SECRET_KEY in .env")
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def _idempotency_key(user_id: int, package: str) -> str:
    """Deterministic idempotency key: prevents duplicate charges on retry."""
    raw = f"zenflow-token-purchase-{user_id}-{package}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ─────────────────────────────────────────
#  CREATE PAYMENT INTENT
# ─────────────────────────────────────────
async def create_payment_intent(
    user_id: int,
    package: str,
    email: str,
) -> dict:
    """
    Creates a Stripe PaymentIntent.
    Returns {client_secret, payment_intent_id, amount, currency}.
    The frontend uses client_secret with Stripe.js to collect card details.
    """
    if package not in PACKAGES:
        raise ValueError(f"Invalid package '{package}'. Choose from: {list(PACKAGES.keys())}")

    pkg = PACKAGES[package]
    currency = PAYMENT_CURRENCY

    if not STRIPE_ENABLED:
        # Mock for development
        mock_id = f"pi_mock_{user_id}_{package}"
        logger.info(f"[Stripe MOCK] PaymentIntent: {mock_id} for {email}")
        return {
            "client_secret":      f"{mock_id}_secret_mock",
            "payment_intent_id":  mock_id,
            "amount":             pkg["price_sgd"] if currency == "sgd" else int(pkg["price_usd"] * 100),
            "currency":           currency,
            "tokens":             pkg["tokens"],
            "label":              pkg["label"],
            "mock":               True,
        }

    import stripe
    _get_stripe()

    amount = pkg["price_sgd"] if currency == "sgd" else int(pkg["price_usd"] * 100)

    intent = stripe.PaymentIntent.create(
        amount=amount,
        currency=currency,
        automatic_payment_methods={"enabled": True},
        receipt_email=email,
        metadata={
            "user_id":  str(user_id),
            "package":  package,
            "tokens":   str(pkg["tokens"]),
            "platform": "zenflow",
        },
        description=pkg["label"],
        idempotency_key=_idempotency_key(user_id, package),
    )

    logger.info(f"Stripe PaymentIntent created: {intent.id} for user {user_id}")
    return {
        "client_secret":     intent.client_secret,
        "payment_intent_id": intent.id,
        "amount":            intent.amount,
        "currency":          intent.currency,
        "tokens":            pkg["tokens"],
        "label":             pkg["label"],
    }


# ─────────────────────────────────────────
#  WEBHOOK HANDLER
# ─────────────────────────────────────────
async def handle_stripe_webhook(payload: bytes, sig_header: str, db) -> dict:
    """
    Process Stripe webhook events.
    Called by POST /webhooks/stripe.
    Verifies signature, then handles payment_intent.succeeded.
    """
    if not STRIPE_ENABLED:
        raise ValueError("Stripe not configured")

    stripe = _get_stripe()

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise ValueError("Invalid Stripe webhook signature")

    logger.info(f"Stripe webhook: {event['type']} ({event['id']})")

    if event["type"] == "payment_intent.succeeded":
        pi      = event["data"]["object"]
        pi_id   = pi["id"]
        meta    = pi.get("metadata", {})
        user_id = int(meta.get("user_id", 0))
        package = meta.get("package", "")
        tokens  = int(meta.get("tokens", 0))

        if not user_id or not package:
            logger.error(f"Webhook missing metadata: {meta}")
            return {"status": "ignored", "reason": "missing metadata"}

        pkg = PACKAGES.get(package, {})
        price_usd = pkg.get("price_usd", 0)

        # Check not already processed (idempotency)
        existing = await db.fetchrow(
            "SELECT id FROM payments WHERE stripe_pi_id = $1", pi_id
        )
        if existing:
            logger.info(f"Webhook: payment {pi_id} already processed")
            return {"status": "duplicate", "payment_id": existing["id"]}

        # Record payment (trigger in DB will credit tokens)
        row = await db.fetchrow("""
            INSERT INTO payments
              (user_id, payment_type, amount_usd, tokens_granted,
               payment_method, stripe_pi_id, stripe_status, status)
            VALUES ($1, 'token_purchase', $2, $3, 'stripe', $4, 'succeeded', 'completed')
            RETURNING id
        """, user_id, price_usd, tokens, pi_id)

        logger.info(f"Payment recorded: id={row['id']} user={user_id} tokens={tokens}")
        return {
            "status":     "processed",
            "payment_id": row["id"],
            "user_id":    user_id,
            "tokens":     tokens,
        }

    elif event["type"] == "payment_intent.payment_failed":
        pi    = event["data"]["object"]
        pi_id = pi["id"]
        err   = pi.get("last_payment_error", {}).get("message", "unknown")
        logger.warning(f"Payment failed: {pi_id} — {err}")
        await db.execute("""
            UPDATE payments SET status='failed', stripe_status=$1
            WHERE stripe_pi_id = $2
        """, "payment_failed", pi_id)
        return {"status": "failed", "pi_id": pi_id, "error": err}

    elif event["type"] == "charge.refunded":
        charge = event["data"]["object"]
        pi_id  = charge.get("payment_intent")
        if pi_id:
            await db.execute("""
                UPDATE payments SET status='refunded', stripe_status='refunded'
                WHERE stripe_pi_id = $1
            """, pi_id)
            logger.info(f"Refund processed for PI: {pi_id}")
        return {"status": "refunded"}

    # Unhandled event type — just acknowledge
    return {"status": "unhandled", "type": event["type"]}


# ─────────────────────────────────────────
#  PACKAGE INFO (public, no auth needed)
# ─────────────────────────────────────────
def get_packages(currency: str = "sgd") -> list[dict]:
    result = []
    for pkg_id, pkg in PACKAGES.items():
        result.append({
            "id":       pkg_id,
            "tokens":   pkg["tokens"],
            "label":    pkg["label"],
            "price":    pkg["price_sgd"] / 100 if currency == "sgd" else pkg["price_usd"],
            "currency": currency.upper(),
            "per_token": round(
                (pkg["price_sgd"] / 100 / pkg["tokens"]) if currency == "sgd"
                else (pkg["price_usd"] / pkg["tokens"]), 2
            ),
        })
    return result
