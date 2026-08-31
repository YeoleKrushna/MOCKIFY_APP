from datetime import datetime, timedelta
from functools import wraps
import json

from flask import Blueprint, jsonify, request, session

from database import db, User, Mock, Result, OTPEvent

admin_bp = Blueprint("admin", __name__)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def admin_required(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Not authenticated"}), 401
        if not user.is_admin:
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return decorated


def super_admin_required(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Not authenticated"}), 401
        if not user.is_super_admin:
            return jsonify({"error": "Super Admin access required"}), 403
        return fn(*args, **kwargs)
    return decorated


@admin_bp.route("/stats", methods=["GET"])
@admin_required
def get_stats():
    total_users = User.query.filter_by(is_admin=False).count()
    total_mocks = Mock.query.count()
    total_results = Result.query.count()
    avg_score = db.session.query(db.func.avg(Result.score)).scalar() or 0

    active_cutoff = datetime.utcnow() - timedelta(minutes=5)
    active_now = User.query.filter(
        User.is_admin.is_(False),
        User.last_seen_at.isnot(None),
        User.last_seen_at >= active_cutoff,
    ).count()

    return jsonify({
        "total_users": total_users,
        "total_mocks": total_mocks,
        "total_results": total_results,
        "avg_score": round(float(avg_score), 1),
        "active_now": active_now,
    }), 200


@admin_bp.route("/users", methods=["GET"])
@super_admin_required
def list_users():
    users = (
        User.query
        .filter_by(is_admin=False)
        .order_by(User.created_at.desc())
        .all()
    )

    result = []

    for user in users:
        user.reset_daily_count_if_needed()

        mock_count = Mock.query.filter_by(user_id=user.id).count()
        result_count = Result.query.filter_by(user_id=user.id).count()

        avg_score = (
            db.session.query(db.func.avg(Result.score))
            .filter(Result.user_id == user.id)
            .scalar()
            or 0
        )

        best_score = (
            db.session.query(db.func.max(Result.score))
            .filter(Result.user_id == user.id)
            .scalar()
        )

        result.append({
            **user.to_dict(),
            "total_mocks": mock_count,
            "completed_tests": result_count,
            "average_score": round(float(avg_score), 1),
            "best_score": best_score if best_score is not None else None,
            "questions_answered": sum(
                r.total or 0
                for r in Result.query.filter_by(user_id=user.id).all()
            ),
        })

    return jsonify({"users": result}), 200


@admin_bp.route("/users/<int:user_id>", methods=["GET"])
@super_admin_required
def user_detail(user_id):
    user = User.query.get(user_id)

    if not user or user.is_admin:
        return jsonify({"error": "User not found"}), 404

    user.reset_daily_count_if_needed()

    mocks = (
        Mock.query
        .filter_by(user_id=user.id)
        .order_by(Mock.created_at.desc())
        .limit(50)
        .all()
    )

    results = (
        Result.query
        .filter_by(user_id=user.id)
        .order_by(Result.timestamp.desc())
        .limit(50)
        .all()
    )

    avg_score = (
        db.session.query(db.func.avg(Result.score))
        .filter(Result.user_id == user.id)
        .scalar()
        or 0
    )

    best_score = (
        db.session.query(db.func.max(Result.score))
        .filter(Result.user_id == user.id)
        .scalar()
    )

    return jsonify({
        "user": user.to_dict(),
        "summary": {
            "total_mocks": Mock.query.filter_by(user_id=user.id).count(),
            "completed_tests": Result.query.filter_by(user_id=user.id).count(),
            "average_score": round(float(avg_score), 1),
            "best_score": best_score,
            "questions_answered": sum(r.total or 0 for r in results),
            "correct_answers": sum(r.correct_answers or 0 for r in results),
            "wrong_answers": sum(r.wrong_answers or 0 for r in results),
        },
        "mocks": [
            {
                "id": m.id,
                "topic": m.topic,
                "timer_minutes": m.timer_minutes or 15,
                "created_at": m.created_at.isoformat(),
            }
            for m in mocks
        ],
        "results": [
            {
                "id": r.id,
                "mock_id": r.mock_id,
                "score": r.score,
                "total": r.total,
                "percentage": round((r.score / r.total) * 100, 1) if r.total else 0,
                "correct_answers": r.correct_answers,
                "wrong_answers": r.wrong_answers,
                "time_taken": r.time_taken,
                "timestamp": r.timestamp.isoformat(),
            }
            for r in results
        ],
    }), 200


@admin_bp.route("/users/<int:user_id>/limit", methods=["PUT"])
@super_admin_required
def update_limit(user_id):
    user = User.query.get(user_id)

    if not user or user.is_admin:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    limit = data.get("daily_mock_limit")

    if not isinstance(limit, int) or limit < 0 or limit > 999:
        return jsonify({"error": "Invalid limit value"}), 400

    user.daily_mock_limit = limit
    db.session.commit()

    return jsonify({
        "message": "Limit updated",
        "user": user.to_dict(),
    }), 200


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@super_admin_required
def delete_user(user_id):
    user = User.query.get(user_id)

    if not user:
        return jsonify({"error": "User not found"}), 404

    if user.is_admin or user.is_super_admin:
        return jsonify({"error": "Cannot delete an admin user"}), 403

    db.session.delete(user)
    db.session.commit()

    return jsonify({"message": "User and all related data deleted"}), 200


@admin_bp.route("/mocks", methods=["GET"])
@admin_required
def list_mocks():
    mocks = (
        Mock.query
        .order_by(Mock.created_at.desc())
        .limit(100)
        .all()
    )

    result = []

    for mock in mocks:
        user = User.query.get(mock.user_id)

        result.append({
            "id": mock.id,
            "user_name": user.name if user else "Unknown",
            "user_email": user.email if user else "Unknown",
            "topic": mock.topic,
            "timer_minutes": mock.timer_minutes or 15,
            "created_at": mock.created_at.isoformat(),
        })

    return jsonify({"mocks": result}), 200


@admin_bp.route("/results", methods=["GET"])
@admin_required
def list_results():
    results = (
        Result.query
        .order_by(Result.timestamp.desc())
        .limit(100)
        .all()
    )

    data = []

    for result in results:
        user = User.query.get(result.user_id)
        mock = Mock.query.get(result.mock_id)

        data.append({
            "id": result.id,
            "user_id": result.user_id,
            "user_name": user.name if user else "Unknown",
            "user_email": user.email if user else "Unknown",
            "topic": mock.topic if mock else "Unknown",
            "score": result.score,
            "total": result.total,
            "percentage": round((result.score / result.total) * 100, 1) if result.total else 0,
            "timestamp": result.timestamp.isoformat(),
        })

    return jsonify({"results": data}), 200


@admin_bp.route("/otp-events", methods=["GET"])
@super_admin_required
def otp_events():
    """Show delivery/request metadata without exposing plaintext OTPs."""
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100

    limit = max(1, min(limit, 250))

    events = (
        OTPEvent.query
        .order_by(OTPEvent.requested_at.desc())
        .limit(limit)
        .all()
    )

    return jsonify({
        "events": [
            {
                "id": event.id,
                "email": event.email,
                "event_type": event.event_type,
                "status": event.status,
                "brevo_message_id": event.brevo_message_id,
                "requested_at": event.requested_at.isoformat(),
            }
            for event in events
        ]
    }), 200

# Backward-compatible helper name used by some older code.
@admin_bp.route("/me", methods=["GET"])
@admin_required
def admin_me():
    user = current_user()
    return jsonify({
        "user": user.to_dict(),
        "is_super_admin": bool(user.is_super_admin),
    }), 200
