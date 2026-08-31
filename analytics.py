from datetime import datetime, timedelta

from flask import Blueprint, jsonify, session

from database import db, User, Mock, Result, OTPEvent

analytics_bp = Blueprint("analytics", __name__)

ACTIVE_WINDOW_MINUTES = 5


@analytics_bp.route("/public-stats", methods=["GET"])
def public_stats():
    """Safe aggregate stats for the public website."""
    now = datetime.utcnow()
    active_cutoff = now - timedelta(minutes=ACTIVE_WINDOW_MINUTES)

    total_users = User.query.filter_by(is_admin=False).count()
    total_mocks = Mock.query.count()
    total_results = Result.query.count()
    active_users = (
        User.query
        .filter(
            User.is_admin.is_(False),
            User.last_seen_at.isnot(None),
            User.last_seen_at >= active_cutoff,
        )
        .count()
    )

    return jsonify({
        "users": total_users,
        "mocks": total_mocks,
        "completed_tests": total_results,
        "active_now": active_users,
        "active_window_minutes": ACTIVE_WINDOW_MINUTES,
    }), 200


@analytics_bp.route("/heartbeat", methods=["POST"])
def heartbeat():
    """Update last-seen timestamp for a logged-in user."""
    user_id = session.get("user_id")

    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401

    user = db.session.get(User, user_id)

    if not user:
        session.clear()
        return jsonify({"error": "User not found"}), 404

    user.last_seen_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"ok": True}), 200


@analytics_bp.route("/private-stats", methods=["GET"])
def private_stats():
    """Detailed aggregate statistics used by the Super Admin panel."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401

    user = db.session.get(User, user_id)
    if not user or not user.is_super_admin:
        return jsonify({"error": "Super Admin access required"}), 403

    now = datetime.utcnow()
    active_cutoff = now - timedelta(minutes=ACTIVE_WINDOW_MINUTES)
    day_cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = User.query.filter_by(is_admin=False).count()
    total_mocks = Mock.query.count()
    total_results = Result.query.count()
    active_now = User.query.filter(
        User.is_admin.is_(False),
        User.last_seen_at.isnot(None),
        User.last_seen_at >= active_cutoff,
    ).count()
    active_today = User.query.filter(
        User.is_admin.is_(False),
        User.last_seen_at.isnot(None),
        User.last_seen_at >= day_cutoff,
    ).count()
    mocks_today = Mock.query.filter(Mock.created_at >= day_cutoff).count()
    results_today = Result.query.filter(Result.timestamp >= day_cutoff).count()
    otp_today = OTPEvent.query.filter(OTPEvent.requested_at >= day_cutoff).count()

    average_score = db.session.query(db.func.avg(Result.score)).scalar() or 0

    return jsonify({
        "total_users": total_users,
        "total_mocks": total_mocks,
        "total_results": total_results,
        "active_now": active_now,
        "active_today": active_today,
        "mocks_today": mocks_today,
        "results_today": results_today,
        "otp_requests_today": otp_today,
        "average_score": round(float(average_score), 1),
        "active_window_minutes": ACTIVE_WINDOW_MINUTES,
    }), 200
