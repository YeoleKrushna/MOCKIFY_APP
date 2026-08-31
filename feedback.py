"""Authenticated user feedback for Mockify.

Feedback is stored in the database and forwarded to the site owner through
Brevo's transactional email API. An optional PNG/JPEG/WebP screenshot is sent
as a base64 attachment; no uploaded image is stored on the server filesystem.
"""

import base64
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

import requests
from flask import Blueprint, jsonify, request, session
from markupsafe import escape

from database import db, Feedback, User

feedback_bp = Blueprint("feedback", __name__)

OWNER_EMAIL = os.environ.get("FEEDBACK_TO_EMAIL", "yeoleagency@gmail.com").strip()
MAX_MESSAGE_CHARS = 2000
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_DAILY_SUBMISSIONS = 3
RATE_WINDOW_SECONDS = 24 * 60 * 60

ALLOWED_IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

SUBMISSION_TIMES = defaultdict(deque)


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _ip_allowed():
    now = time.time()
    q = SUBMISSION_TIMES[_client_ip()]
    while q and q[0] <= now - RATE_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= MAX_DAILY_SUBMISSIONS:
        return False
    q.append(now)
    return True


def _category_label(category):
    return {
        "bug": "Bug / Error",
        "suggestion": "Suggestion",
        "question": "Question",
        "general": "Other",
    }.get(category, "Other")


def _send_feedback_email(user, feedback, image_bytes=None):
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM", "otp@mockify.tech").strip()
    sender_name = os.environ.get("MAIL_FROM_NAME", "Mockify").strip() or "Mockify"

    if not api_key or not sender or not OWNER_EMAIL:
        raise RuntimeError("Feedback email configuration is incomplete.")

    safe_message = str(escape(feedback.message)).replace("\n", "<br>")
    category = _category_label(feedback.category)

    html = f"""
    <html>
      <body style="font-family:Arial,sans-serif;line-height:1.6;color:#1c1c27;">
        <h2>New Mockify Feedback</h2>
        <p><strong>From:</strong> {escape(user.name)} ({escape(user.email)})</p>
        <p><strong>Category:</strong> {escape(category)}</p>
        <p><strong>Submitted:</strong> {feedback.created_at.isoformat()}</p>
        <hr>
        <p>{safe_message}</p>
        {"<p>Screenshot attached.</p>" if image_bytes else ""}
      </body>
    </html>
    """

    payload = {
        "sender": {"name": sender_name, "email": sender},
        "to": [{"email": OWNER_EMAIL, "name": "Mockify Owner"}],
        "replyTo": {"email": user.email, "name": user.name[:70]},
        "subject": f"Mockify Feedback — {category}",
        "htmlContent": html,
        "textContent": (
            f"New Mockify Feedback\n\n"
            f"From: {user.name} ({user.email})\n"
            f"Category: {category}\n"
            f"Submitted: {feedback.created_at.isoformat()}\n\n"
            f"{feedback.message}"
        ),
    }

    if image_bytes and feedback.image_name:
        payload["attachment"] = [{
            "content": base64.b64encode(image_bytes).decode("ascii"),
            "name": feedback.image_name,
        }]

    response = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json",
        },
        json=payload,
        timeout=20,
    )

    if not response.ok:
        raise RuntimeError("Brevo rejected the feedback email.")

    return response.json().get("messageId")


@feedback_bp.route("/submit", methods=["POST"])
def submit_feedback():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please sign in to send feedback."}), 401

    user = db.session.get(User, user_id)
    if not user:
        session.clear()
        return jsonify({"error": "User account not found."}), 404

    if not _ip_allowed():
        return jsonify({"error": "Feedback limit reached. You can send up to 3 feedback messages per day."}), 429

    message = (request.form.get("message") or "").strip()
    category = (request.form.get("category") or "general").strip().lower()

    if category not in {"bug", "suggestion", "question", "general"}:
        return jsonify({"error": "Invalid feedback category."}), 400

    if not message:
        return jsonify({"error": "Please write a feedback message."}), 400

    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": "Feedback is limited to 2,000 characters."}), 400

    existing_today = Feedback.query.filter(
        Feedback.user_id == user.id,
        Feedback.created_at >= datetime.utcnow() - timedelta(days=1),
    ).count()

    if existing_today >= MAX_DAILY_SUBMISSIONS:
        return jsonify({"error": "You have reached the daily feedback limit of 3."}), 429

    upload = request.files.get("image")
    image_bytes = None
    feedback_image_name = None
    mime = None

    if upload and upload.filename:
        mime = (upload.mimetype or "").lower()

        if mime not in ALLOWED_IMAGE_TYPES:
            return jsonify({"error": "Only PNG, JPG, or WebP images are allowed."}), 400

        image_bytes = upload.read(MAX_IMAGE_BYTES + 1)

        if len(image_bytes) > MAX_IMAGE_BYTES:
            return jsonify({"error": "The image must be 2 MB or smaller."}), 400

        original = os.path.basename(upload.filename).strip()
        extension = ALLOWED_IMAGE_TYPES[mime]
        feedback_image_name = (original[:200] or f"feedback.{extension}")

    feedback = Feedback(
        user_id=user.id,
        name=user.name,
        email=user.email,
        category=category,
        message=message,
        image_name=feedback_image_name,
        image_mime=mime,
        image_size=len(image_bytes) if image_bytes else None,
        status="pending",
    )

    db.session.add(feedback)
    db.session.commit()

    try:
        message_id = _send_feedback_email(
            user,
            feedback,
            image_bytes=image_bytes,
        )

        feedback.status = "sent"
        db.session.commit()

        return jsonify({
            "message": "Feedback sent successfully.",
            "feedback": {
                "id": feedback.id,
                "category_label": _category_label(feedback.category),
                "message": feedback.message,
                "status": feedback.status,
                "created_at": feedback.created_at.isoformat(),
                "has_image": bool(feedback.image_name),
            },
            "message_id": message_id,
        }), 201

    except Exception:
        feedback.status = "saved"
        db.session.commit()

        return jsonify({
            "error": "Your feedback was saved, but the email could not be delivered right now. Please try again later."
        }), 503


@feedback_bp.route("/mine", methods=["GET"])
def my_feedback():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated."}), 401

    rows = (
        Feedback.query
        .filter_by(user_id=user_id)
        .order_by(Feedback.created_at.desc())
        .limit(10)
        .all()
    )

    return jsonify({
        "feedback": [
            {
                "id": row.id,
                "category": row.category,
                "category_label": _category_label(row.category),
                "message": row.message,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "has_image": bool(row.image_name),
            }
            for row in rows
        ]
    }), 200
