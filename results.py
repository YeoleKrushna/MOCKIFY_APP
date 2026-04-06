from flask import Blueprint, request, jsonify, session
from database import db, User, Mock, Result
import json

results_bp = Blueprint('results', __name__)

@results_bp.route('/submit', methods=['POST'])
def submit_result():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    data = request.get_json()
    mock_id = data.get('mock_id')
    user_answers = data.get('answers', {})  # {"0": "A", "1": "C", ...}
    time_taken = data.get('time_taken', 0)

    mock = Mock.query.get(mock_id)
    if not mock or mock.user_id != user_id:
        return jsonify({'error': 'Mock not found'}), 404

    questions = json.loads(mock.questions)
    correct = 0
    wrong = 0
    detailed = []

    for i, q in enumerate(questions):
        user_ans = user_answers.get(str(i), None)
        correct_ans = q['answer']
        is_correct = user_ans == correct_ans
        if is_correct:
            correct += 1
        else:
            wrong += 1
        detailed.append({
            'question': q['question'],
            'options': q['options'],
            'correct_answer': correct_ans,
            'user_answer': user_ans,
            'is_correct': is_correct
        })

    result = Result(
        user_id=user_id,
        mock_id=mock_id,
        score=correct,
        total=len(questions),
        correct_answers=correct,
        wrong_answers=wrong,
        user_answers=json.dumps(user_answers),
        time_taken=time_taken
    )
    db.session.add(result)
    db.session.commit()

    return jsonify({
        'result_id': result.id,
        'score': correct,
        'total': len(questions),
        'percentage': round((correct / len(questions)) * 100, 1),
        'correct_answers': correct,
        'wrong_answers': wrong,
        'time_taken': time_taken,
        'detailed': detailed,
        'topic': mock.topic
    }), 201

@results_bp.route('/<int:result_id>', methods=['GET'])
def get_result(result_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    result = Result.query.get(result_id)
    if not result or result.user_id != user_id:
        return jsonify({'error': 'Result not found'}), 404

    mock = Mock.query.get(result.mock_id)
    questions = json.loads(mock.questions)
    user_answers = json.loads(result.user_answers)

    detailed = []
    for i, q in enumerate(questions):
        user_ans = user_answers.get(str(i), None)
        correct_ans = q['answer']
        detailed.append({
            'question': q['question'],
            'options': q['options'],
            'correct_answer': correct_ans,
            'user_answer': user_ans,
            'is_correct': user_ans == correct_ans
        })

    return jsonify({
        'result_id': result.id,
        'score': result.score,
        'total': result.total,
        'percentage': round((result.score / result.total) * 100, 1),
        'correct_answers': result.correct_answers,
        'wrong_answers': result.wrong_answers,
        'time_taken': result.time_taken,
        'timestamp': result.timestamp.isoformat(),
        'topic': mock.topic,
        'detailed': detailed
    }), 200

@results_bp.route('/history', methods=['GET'])
def result_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    results = Result.query.filter_by(user_id=user_id).order_by(Result.timestamp.desc()).limit(20).all()
    history = []
    for r in results:
        mock = Mock.query.get(r.mock_id)
        history.append({
            'result_id': r.id,
            'topic': mock.topic if mock else 'Unknown',
            'score': r.score,
            'total': r.total,
            'percentage': round((r.score / r.total) * 100, 1),
            'timestamp': r.timestamp.isoformat()
        })
    return jsonify({'history': history}), 200
