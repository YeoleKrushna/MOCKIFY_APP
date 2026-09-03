"""Private administrator and Super Admin endpoints."""
from functools import wraps

from flask import Blueprint, jsonify, request, session
from database import db, Mock, OTPEvent, Result, User

admin_bp = Blueprint("admin", __name__)


def current_user():
    return db.session.get(User, session.get("user_id")) if session.get("user_id") else None


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Not authenticated"}), 401
        if not (user.is_admin or user.is_super_admin):
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapped


def super_admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Not authenticated"}), 401
        if not user.is_super_admin:
            return jsonify({"error": "Super Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapped


@admin_bp.route("/stats")
@admin_required
def get_stats():
    return jsonify({"total_users": User.query.filter_by(is_admin=False).count(), "total_mocks": Mock.query.count(), "total_results": Result.query.count(), "avg_score": round(float(db.session.query(db.func.avg(Result.score)).scalar() or 0), 1)}), 200


def user_metrics(user):
    results = Result.query.filter_by(user_id=user.id).all()
    average = db.session.query(db.func.avg(Result.score)).filter_by(user_id=user.id).scalar() or 0
    best = db.session.query(db.func.max(Result.score)).filter_by(user_id=user.id).scalar()
    return {**user.to_dict(), "total_mocks": Mock.query.filter_by(user_id=user.id).count(), "completed_tests": len(results), "average_score": round(float(average), 1), "best_score": best, "questions_answered": sum(result.total or 0 for result in results), "correct_answers": sum(result.correct_answers or 0 for result in results), "wrong_answers": sum(result.wrong_answers or 0 for result in results)}


@admin_bp.route("/users")
@admin_required
def list_users():
    users = User.query.filter_by(is_admin=False).order_by(User.created_at.desc()).all()
    return jsonify({"users": [user_metrics(user) for user in users]}), 200


@admin_bp.route("/users/<int:user_id>")
@super_admin_required
def user_detail(user_id):
    user = db.session.get(User, user_id)
    if not user or user.is_admin or user.is_super_admin:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"user": user.to_dict(), "summary": user_metrics(user)}), 200


@admin_bp.route("/users/<int:user_id>/limit", methods=["PUT"])
@super_admin_required
def update_limit(user_id):
    user = db.session.get(User, user_id)
    limit = (request.get_json(silent=True) or {}).get("daily_mock_limit")
    if not user or user.is_admin or user.is_super_admin:
        return jsonify({"error": "User not found"}), 404
    if not isinstance(limit, int) or not 0 <= limit <= 999:
        return jsonify({"error": "Invalid limit value"}), 400
    user.daily_mock_limit = limit
    db.session.commit()
    return jsonify({"message": "Limit updated", "user": user.to_dict()}), 200


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@super_admin_required
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if user.is_admin or user.is_super_admin:
        return jsonify({"error": "Cannot delete an admin user"}), 403
    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": "User and related data deleted"}), 200


@admin_bp.route("/results/<int:result_id>", methods=["DELETE"])
@admin_required
def delete_result(result_id):
    """Remove one result entry without deleting the user's account or mock."""
    result = db.session.get(Result, result_id)
    if not result:
        return jsonify({"error": "Result not found"}), 404

    db.session.delete(result)
    db.session.commit()
    return jsonify({"message": "Result deleted"}), 200


@admin_bp.route("/mocks")
@admin_required
def list_mocks():
    mocks = Mock.query.order_by(Mock.created_at.desc()).limit(100).all()
    return jsonify({"mocks": [{"id": m.id, "user_name": m.user.name if m.user else "Unknown", "user_email": m.user.email if m.user else "Unknown", "topic": m.topic, "timer_minutes": m.timer_minutes or 15, "created_at": m.created_at.isoformat()} for m in mocks]}), 200


@admin_bp.route("/results")
@admin_required
def list_results():
    rows = Result.query.order_by(Result.timestamp.desc()).limit(100).all()
    return jsonify({"results": [{"id": r.id, "user_name": r.user.name if r.user else "Unknown", "user_email": r.user.email if r.user else "Unknown", "topic": r.mock.topic if r.mock else "Unknown", "score": r.score, "total": r.total, "percentage": round(r.score / r.total * 100, 1) if r.total else 0, "timestamp": r.timestamp.isoformat()} for r in rows]}), 200


@admin_bp.route("/otp-events")
@super_admin_required
def otp_events():
    try:
        limit = max(1, min(int(request.args.get("limit", "100")), 250))
    except ValueError:
        limit = 100
    events = OTPEvent.query.order_by(OTPEvent.requested_at.desc()).limit(limit).all()
    return jsonify({"events": [{"id": e.id, "email": e.email, "event_type": e.event_type, "status": e.status, "brevo_message_id": e.brevo_message_id, "requested_at": e.requested_at.isoformat()} for e in events]}), 200
