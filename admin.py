from flask import Blueprint, request, jsonify, session
from database import db, User, Mock, Result
from functools import wraps

admin_bp = Blueprint('admin', __name__)

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': 'Not authenticated'}), 401
        user = User.query.get(user_id)
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated

@admin_bp.route('/stats', methods=['GET'])
@admin_required
def get_stats():
    total_users = User.query.filter_by(is_admin=False).count()
    total_mocks = Mock.query.count()
    total_results = Result.query.count()
    avg_score = db.session.query(db.func.avg(Result.score)).scalar() or 0
    return jsonify({
        'total_users': total_users,
        'total_mocks': total_mocks,
        'total_results': total_results,
        'avg_score': round(float(avg_score), 1)
    }), 200

@admin_bp.route('/users', methods=['GET'])
@admin_required
def list_users():
    users = User.query.filter_by(is_admin=False).order_by(User.created_at.desc()).all()
    result = []
    for u in users:
        u.reset_daily_count_if_needed()
        mock_count = Mock.query.filter_by(user_id=u.id).count()
        result.append({**u.to_dict(), 'total_mocks': mock_count})
    return jsonify({'users': result}), 200

@admin_bp.route('/users/<int:user_id>/limit', methods=['PUT'])
@admin_required
def update_limit(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.get_json()
    limit = data.get('daily_mock_limit')
    if limit is None or not isinstance(limit, int) or limit < 0:
        return jsonify({'error': 'Invalid limit value'}), 400
    user.daily_mock_limit = limit
    db.session.commit()
    return jsonify({'message': 'Limit updated', 'user': user.to_dict()}), 200

@admin_bp.route('/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    if user.is_admin:
        return jsonify({'error': 'Cannot delete admin'}), 403
    Result.query.filter_by(user_id=user_id).delete()
    Mock.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()
    return jsonify({'message': 'User deleted'}), 200

@admin_bp.route('/mocks', methods=['GET'])
@admin_required
def list_mocks():
    mocks = Mock.query.order_by(Mock.created_at.desc()).limit(50).all()
    result = []
    for m in mocks:
        user = User.query.get(m.user_id)
        result.append({
            'id': m.id,
            'user_name': user.name if user else 'Unknown',
            'user_email': user.email if user else 'Unknown',
            'topic': m.topic,
            'created_at': m.created_at.isoformat()
        })
    return jsonify({'mocks': result}), 200

@admin_bp.route('/results', methods=['GET'])
@admin_required
def list_results():
    results = Result.query.order_by(Result.timestamp.desc()).limit(50).all()
    data = []
    for r in results:
        user = User.query.get(r.user_id)
        mock = Mock.query.get(r.mock_id)
        data.append({
            'id': r.id,
            'user_name': user.name if user else 'Unknown',
            'topic': mock.topic if mock else 'Unknown',
            'score': r.score,
            'total': r.total,
            'percentage': round((r.score / r.total) * 100, 1),
            'timestamp': r.timestamp.isoformat()
        })
    return jsonify({'results': data}), 200
