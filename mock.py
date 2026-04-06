from flask import Blueprint, request, jsonify, session
from database import db, User, Mock
import requests
import json
import os
import time
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

mock_bp = Blueprint('mock', __name__)

load_dotenv(override=True)

GROQ_API_URL = os.environ.get('GROQ_API_URL', 'https://api.groq.com/openai/v1/chat/completions').strip()
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile').strip()
GROQ_MAX_OUTPUT_TOKENS = int(os.environ.get('GROQ_MAX_OUTPUT_TOKENS', '1500'))
GROQ_MAX_RETRIES = max(1, int(os.environ.get('GROQ_MAX_RETRIES', '3')))
GROQ_RETRY_BASE_DELAY = max(1, int(os.environ.get('GROQ_RETRY_BASE_DELAY', '2')))
GENERATE_COOLDOWN_SECONDS = max(1, int(os.environ.get('GENERATE_COOLDOWN_SECONDS', '15')))
MOCK_CACHE_WINDOW_SECONDS = max(0, int(os.environ.get('MOCK_CACHE_WINDOW_SECONDS', '1800')))

last_generate_attempts = {}


def get_groq_api_key():
    api_key = os.environ.get('GROQ_API_KEY', '').strip()
    if not api_key:
        raise ValueError("GROQ_API_KEY not configured")
    return api_key

MOCK_PROMPT = """You are an expert mock test generator.

User input: {user_prompt}

Task:
- Understand topic from input
- Infer difficulty (default = medium)
- Generate exactly 10 MCQs

Rules:
- Each question must have a question, 4 options (A, B, C, D), and 1 correct answer
- No repetition
- Clear exam-style questions

Output ONLY valid JSON with no markdown, no extra text:
{{
  "questions": [
    {{
      "question": "...",
      "options": {{
        "A": "...",
        "B": "...",
        "C": "...",
        "D": "..."
      }},
      "answer": "A"
    }}
  ]
}}"""


class GroqAPIError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_topic_label(topic):
    cleaned = re.sub(r'\s+', ' ', topic).strip()
    return cleaned[:120] if cleaned else 'the given topic'


def build_fallback_questions(topic):
    topic_label = normalize_topic_label(topic)
    prompts = [
        (
            f"What is the primary goal of studying {topic_label}?",
            {
                "A": f"To understand the core concepts and practical use of {topic_label}",
                "B": "To avoid using the topic in real projects",
                "C": "To replace every other subject completely",
                "D": "To memorize unrelated historical facts"
            },
            "A"
        ),
        (
            f"Which approach is best when beginning a new chapter in {topic_label}?",
            {
                "A": "Start with fundamentals, examples, and regular practice",
                "B": "Skip theory and guess advanced answers",
                "C": "Only read summaries and never solve questions",
                "D": "Ignore definitions and key terminology"
            },
            "A"
        ),
        (
            f"In most assessments, strong understanding of {topic_label} is shown by:",
            {
                "A": "Applying concepts correctly to new problems",
                "B": "Memorizing only one keyword from the topic",
                "C": "Avoiding all problem solving",
                "D": "Writing answers unrelated to the question"
            },
            "A"
        ),
        (
            f"When revising {topic_label}, which habit improves retention the most?",
            {
                "A": "Practicing repeatedly with short review cycles",
                "B": "Reading once and never revisiting",
                "C": "Ignoring mistakes after each test",
                "D": "Studying only the night before"
            },
            "A"
        ),
        (
            f"What is the safest way to improve accuracy in {topic_label} MCQs?",
            {
                "A": "Eliminate wrong options and verify the key idea",
                "B": "Choose the longest option every time",
                "C": "Pick the same option letter for all questions",
                "D": "Answer without reading the full question"
            },
            "A"
        ),
        (
            f"Why are examples important while learning {topic_label}?",
            {
                "A": "They connect theory to realistic use cases",
                "B": "They reduce understanding of the basics",
                "C": "They make revision impossible",
                "D": "They remove the need to think critically"
            },
            "A"
        ),
        (
            f"If you get a question wrong in {topic_label}, the best next step is to:",
            {
                "A": "Review the concept and understand why the right answer works",
                "B": "Ignore the topic completely",
                "C": "Memorize the wrong option",
                "D": "Assume the exam key is always incorrect"
            },
            "A"
        ),
        (
            f"Which strategy best supports long-term mastery of {topic_label}?",
            {
                "A": "Consistent practice plus concept revision",
                "B": "Last-minute cramming only",
                "C": "Avoiding tests until the final exam",
                "D": "Studying without checking answers"
            },
            "A"
        ),
        (
            f"What does a well-designed mock test on {topic_label} usually measure?",
            {
                "A": "Conceptual understanding, application, and accuracy",
                "B": "Typing speed only",
                "C": "Luck without preparation",
                "D": "Unrelated personal preferences"
            },
            "A"
        ),
        (
            f"How should you use feedback after practicing {topic_label}?",
            {
                "A": "Identify weak areas and improve them systematically",
                "B": "Delete the result and repeat the same mistakes",
                "C": "Focus only on answered questions you already knew",
                "D": "Stop practicing after one attempt"
            },
            "A"
        ),
    ]
    return {
        "questions": [
            {
                "question": question,
                "options": options,
                "answer": answer
            }
            for question, options, answer in prompts
        ]
    }


def get_generate_cooldown_remaining(user_id):
    now = time.time()
    last_request_time = last_generate_attempts.get(user_id)
    if last_request_time is None:
        return 0

    remaining = GENERATE_COOLDOWN_SECONDS - (now - last_request_time)
    if remaining > 0:
        return int(remaining) if remaining.is_integer() else int(remaining) + 1

    return 0


def mark_generate_attempt(user_id):
    last_generate_attempts[user_id] = time.time()


def get_cached_mock(user_id, topic):
    if MOCK_CACHE_WINDOW_SECONDS <= 0:
        return None

    cutoff = datetime.utcnow() - timedelta(seconds=MOCK_CACHE_WINDOW_SECONDS)
    return (
        Mock.query.filter_by(user_id=user_id, topic=topic)
        .filter(Mock.created_at >= cutoff)
        .order_by(Mock.created_at.desc())
        .first()
    )


def extract_groq_text(message_content):
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        text_parts = []
        for item in message_content:
            if isinstance(item, dict) and item.get('type') == 'text':
                text_parts.append(item.get('text', ''))
        return ''.join(text_parts)
    raise ValueError('Unsupported Groq message content format')


def call_groq_once(prompt):
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You generate exactly 10 MCQs and return only valid JSON."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
        "max_tokens": GROQ_MAX_OUTPUT_TOKENS,
        "stream": False
    }
    try:
        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {get_groq_api_key()}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise GroqAPIError('AI request timed out. Please try again.') from exc
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        error_message = 'AI service request failed. Please try again.'

        if exc.response is not None:
            try:
                error_payload = exc.response.json()
                if isinstance(error_payload, dict):
                    raw_error = error_payload.get('error')
                    if isinstance(raw_error, dict):
                        api_message = raw_error.get('message')
                    elif isinstance(raw_error, str):
                        api_message = raw_error
                    else:
                        api_message = error_payload.get('message')
                    if api_message:
                        error_message = api_message
            except ValueError:
                pass

        if status_code == 429:
            error_message = 'AI service is rate-limited right now. Please wait a minute and try again.'
        elif status_code >= 500:
            error_message = 'AI service is temporarily unavailable. Please try again shortly.'

        raise GroqAPIError(error_message, status_code=status_code) from exc
    except requests.exceptions.RequestException as exc:
        raise GroqAPIError('Could not reach the AI service. Please try again.') from exc

    try:
        data = response.json()
        text = extract_groq_text(data['choices'][0]['message']['content'])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GroqAPIError('AI returned an unexpected response. Please try again.') from exc

    # Strip markdown fences if present
    text = text.strip()
    if text.startswith('```'):
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GroqAPIError('AI returned invalid JSON. Please try again.') from exc


def call_groq(prompt):
    last_error = None
    for attempt in range(GROQ_MAX_RETRIES):
        try:
            return call_groq_once(prompt)
        except GroqAPIError as exc:
            last_error = exc
            should_retry = exc.status_code == 429 and attempt < GROQ_MAX_RETRIES - 1
            if not should_retry:
                raise
            time.sleep(GROQ_RETRY_BASE_DELAY * (attempt + 1))

    if last_error is not None:
        raise last_error

def validate_questions(questions_data):
    if not isinstance(questions_data, dict):
        return False
    questions = questions_data.get('questions', [])
    if len(questions) != 10:
        return False
    for q in questions:
        if not all(k in q for k in ['question', 'options', 'answer']):
            return False
        if not all(k in q['options'] for k in ['A', 'B', 'C', 'D']):
            return False
        if q['answer'] not in ['A', 'B', 'C', 'D']:
            return False
    return True

@mock_bp.route('/generate', methods=['POST'])
def generate_mock():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    if not user.can_take_mock():
        return jsonify({'error': f'Daily limit reached. You can take {user.daily_mock_limit} mock(s) per day.'}), 429

    data = request.get_json()
    topic = data.get('topic', '').strip()
    if not topic:
        return jsonify({'error': 'Topic is required'}), 400
    if len(topic) > 500:
        return jsonify({'error': 'Topic too long (max 500 chars)'}), 400

    cached_mock = get_cached_mock(user_id, topic)
    if cached_mock:
        return jsonify({
            'mock_id': cached_mock.id,
            'topic': topic,
            'questions': json.loads(cached_mock.questions),
            'total': 10,
            'cached': True
        }), 200

    cooldown_remaining = get_generate_cooldown_remaining(user_id)
    if cooldown_remaining > 0:
        return jsonify({
            'error': f'Please wait {cooldown_remaining} seconds before generating another mock.'
        }), 429

    prompt = MOCK_PROMPT.format(user_prompt=topic)
    mark_generate_attempt(user_id)
    used_fallback = False
    try:
        questions_data = call_groq(prompt)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 500
    except GroqAPIError as exc:
        if exc.status_code == 429:
            questions_data = build_fallback_questions(topic)
            used_fallback = True
        else:
            return jsonify({'error': exc.message}), exc.status_code

    if not validate_questions(questions_data):
        return jsonify({'error': 'AI returned invalid format. Please try again.'}), 502

    mock = Mock(
        user_id=user_id,
        topic=topic,
        questions=json.dumps(questions_data['questions'])
    )
    db.session.add(mock)
    user.mocks_taken_today += 1
    db.session.commit()

    return jsonify({
        'mock_id': mock.id,
        'topic': topic,
        'questions': questions_data['questions'],
        'total': 10,
        'fallback': used_fallback
    }), 201

@mock_bp.route('/<int:mock_id>', methods=['GET'])
def get_mock(mock_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    mock = Mock.query.get(mock_id)
    if not mock:
        return jsonify({'error': 'Mock not found'}), 404
    if mock.user_id != user_id:
        user = User.query.get(user_id)
        if not user or not user.is_admin:
            return jsonify({'error': 'Access denied'}), 403

    # Strip answers for exam mode
    questions = json.loads(mock.questions)
    safe_questions = []
    for q in questions:
        safe_questions.append({
            'question': q['question'],
            'options': q['options']
        })

    return jsonify({
        'mock_id': mock.id,
        'topic': mock.topic,
        'questions': safe_questions,
        'created_at': mock.created_at.isoformat()
    }), 200

@mock_bp.route('/history', methods=['GET'])
def mock_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    mocks = Mock.query.filter_by(user_id=user_id).order_by(Mock.created_at.desc()).limit(20).all()
    return jsonify({'mocks': [{'id': m.id, 'topic': m.topic, 'created_at': m.created_at.isoformat()} for m in mocks]}), 200
