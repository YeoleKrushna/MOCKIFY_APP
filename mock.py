from flask import Blueprint, request, jsonify, session
from database import db, User, Mock
import requests
import json
import os
import time
import re
from datetime import datetime, timedelta

from groq_client import GroqAPIError as ClientGroqAPIError, call_groq_http

mock_bp = Blueprint('mock', __name__)

GROQ_API_URL = os.environ.get('GROQ_API_URL', 'https://api.groq.com/openai/v1/chat/completions').strip()
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-20b').strip()
GROQ_MAX_OUTPUT_TOKENS = int(os.environ.get('GROQ_MAX_OUTPUT_TOKENS', '4000'))
GROQ_MAX_RETRIES = max(1, int(os.environ.get('GROQ_MAX_RETRIES', '3')))
GROQ_RETRY_BASE_DELAY = max(1, int(os.environ.get('GROQ_RETRY_BASE_DELAY', '2')))
GENERATE_COOLDOWN_SECONDS = max(1, int(os.environ.get('GENERATE_COOLDOWN_SECONDS', '15')))
MOCK_CACHE_WINDOW_SECONDS = max(0, int(os.environ.get('MOCK_CACHE_WINDOW_SECONDS', '1800')))

last_generate_attempts = {}


MOCK_PROMPT = """You are an expert exam and interview question designer.

Requested topic:
{user_prompt}

Your job is to create ONE high-quality 10-question MCQ mock test that gives the learner broad and useful coverage of the requested topic.

Before writing the questions, silently identify the most important subtopics and dimensions of the requested topic. Then distribute the 10 questions across those dimensions so the test is as comprehensive as possible within exactly 10 questions.

Coverage priorities (adapt to the topic; do not force irrelevant categories):
1. Core definition, purpose, and fundamentals
2. Key concepts, components, terminology, or structure
3. How the topic works / underlying mechanism, workflow, or principles
4. Practical application or implementation
5. Syntax, formulas, commands, APIs, configuration, or technical details when relevant
6. Comparison, trade-offs, or choosing between related concepts when relevant
7. Edge cases, constraints, limitations, or failure modes when relevant
8. Debugging, troubleshooting, output prediction, or scenario-based reasoning when relevant
9. Common mistakes, misconceptions, or exam traps
10. Advanced/interview-level understanding or real-world decision making

Important:
- Exactly 10 questions. Never return 9 or 11.
- Every question must be directly about the requested topic.
- Cover different subtopics; avoid repeated ideas.
- Prefer application, reasoning, and scenario questions over trivial memorization when the topic allows it.
- Difficulty should be mixed: foundational to moderate, with a few challenging questions.
- For narrow topics, adapt the coverage intelligently instead of inventing unrelated material.
- Do not use vague questions merely about "studying" the topic.
- All four options must be plausible enough to require understanding.
- Exactly one option is correct.
- Keep each question and option reasonably concise so all 10 questions fit in the response.

Return ONLY valid JSON. No markdown. No commentary.

Output shape:
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
}}
"""


GroqAPIError = ClientGroqAPIError


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
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.2,
        "max_completion_tokens": GROQ_MAX_OUTPUT_TOKENS,
        "reasoning_format": "hidden",
        "response_format": {
            "type": "json_object",
        },
        "stream": False,
    }

    try:
        data = call_groq_http(payload)
    except GroqAPIError:
        raise
    except Exception as exc:
        raise GroqAPIError(
            "Could not reach the AI service. Please try again.",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        text = data["choices"][0]["message"]["content"]

        if not isinstance(text, str):
            raise ValueError("Groq returned non-text content")

        return json.loads(text)

    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GroqAPIError(
            "AI returned an unexpected JSON response. Please try again.",
            status_code=502,
            retryable=True,
        ) from exc


def call_groq(prompt):
    last_error = None

    for attempt in range(GROQ_MAX_RETRIES):
        try:
            result = call_groq_once(prompt)

            if validate_questions(result):
                return result

            last_error = GroqAPIError(
                "AI generated an incomplete or invalid 10-question mock.",
                status_code=422
            )
            print(
                f"[MOCK] Invalid generation on attempt {attempt + 1}/{GROQ_MAX_RETRIES}.",
                flush=True
            )

        except GroqAPIError as exc:
            last_error = exc
            should_retry = (
                exc.status_code in (422, 429)
                and attempt < GROQ_MAX_RETRIES - 1
            )
            if not should_retry:
                raise

        if attempt < GROQ_MAX_RETRIES - 1:
            delay = GROQ_RETRY_BASE_DELAY * (attempt + 1)
            print(f"[MOCK] Retrying Groq in {delay}s...", flush=True)
            time.sleep(delay)

    if last_error is not None:
        raise last_error

    raise GroqAPIError(
        "AI failed to generate a valid 10-question mock.",
        status_code=502
    )

def validate_questions(questions_data):
    if not isinstance(questions_data, dict):
        return False

    questions = questions_data.get('questions', [])
    if not isinstance(questions, list) or len(questions) != 10:
        return False

    seen = set()

    for q in questions:
        if not isinstance(q, dict):
            return False

        if not all(k in q for k in ['question', 'options', 'answer']):
            return False

        question_text = str(q['question']).strip()
        if not question_text:
            return False

        normalized = re.sub(r'\W+', ' ', question_text.lower()).strip()
        if normalized in seen:
            return False
        seen.add(normalized)

        options = q.get('options')
        if not isinstance(options, dict):
            return False

        if not all(k in options for k in ['A', 'B', 'C', 'D']):
            return False

        if not all(str(options[k]).strip() for k in ['A', 'B', 'C', 'D']):
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
        return jsonify({
            'error': f'Daily limit reached. You can take {user.daily_mock_limit} mock(s) per day.'
        }), 429

    data = request.get_json(silent=True) or {}
    topic = str(data.get('topic', '')).strip()

    if not topic:
        return jsonify({'error': 'Topic is required'}), 400

    if len(topic) > 500:
        return jsonify({'error': 'Topic too long (max 500 chars)'}), 400

    try:
        timer_minutes = int(data.get('timer_minutes'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Please select a valid test duration.'}), 400

    # Presets are a UI convenience; any practical whole-minute session is valid.
    if not 1 <= timer_minutes <= 180:
        return jsonify({'error': 'Test duration must be between 1 and 180 minutes.'}), 400

    cached_mock = get_cached_mock(user_id, topic)
    if cached_mock:
        cached_mock.timer_minutes = timer_minutes
        db.session.commit()

        return jsonify({
            'mock_id': cached_mock.id,
            'topic': topic,
            'questions': json.loads(cached_mock.questions),
            'timer_minutes': timer_minutes,
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

    print(
        f"[MOCK] Generating 10-question mock | user={user_id} | topic={topic!r} | timer={timer_minutes}m",
        flush=True
    )

    used_fallback = False

    try:
        questions_data = call_groq(prompt)

    except ValueError as exc:
        print(f"[MOCK] Configuration error: {exc}", flush=True)
        return jsonify({'error': str(exc)}), 500

    except GroqAPIError as exc:
        print(
            f"[MOCK] Groq error status={exc.status_code}: {exc.message}",
            flush=True
        )
        return jsonify({'error': exc.message}), exc.status_code

    if not validate_questions(questions_data):
        print('[MOCK] Validation failed after all generation attempts.', flush=True)
        return jsonify({
            'error': 'AI could not produce a complete 10-question mock. Please try again.'
        }), 502

    mock = Mock(
        user_id=user_id,
        topic=topic,
        questions=json.dumps(questions_data['questions']),
        timer_minutes=timer_minutes
    )

    db.session.add(mock)
    user.mocks_taken_today += 1
    db.session.commit()

    print(
        f"[MOCK] Success | mock_id={mock.id} | questions={len(questions_data['questions'])} | timer={timer_minutes}m",
        flush=True
    )

    return jsonify({
        'mock_id': mock.id,
        'topic': topic,
        'questions': questions_data['questions'],
        'timer_minutes': timer_minutes,
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
        'timer_minutes': mock.timer_minutes or 15,
        'created_at': mock.created_at.isoformat()
    }), 200

@mock_bp.route('/history', methods=['GET'])
def mock_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    mocks = Mock.query.filter_by(user_id=user_id).order_by(Mock.created_at.desc()).limit(20).all()
    return jsonify({'mocks': [{'id': m.id, 'topic': m.topic, 'created_at': m.created_at.isoformat()} for m in mocks]}), 200
