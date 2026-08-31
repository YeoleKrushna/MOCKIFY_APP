from flask import Blueprint, request, jsonify, session
from database import db, User, Mock, Result

import json
import os
import requests


results_bp = Blueprint("results", __name__)


# =========================================================
# GROQ CONFIG
# =========================================================

GROQ_API_URL = os.environ.get(
    "GROQ_API_URL",
    "https://api.groq.com/openai/v1/chat/completions"
).strip()

GROQ_MODEL = os.environ.get(
    "GROQ_MODEL",
    "openai/gpt-oss-20b"
).strip()

try:
    EXPLANATION_MAX_TOKENS = int(
        os.environ.get(
            "GROQ_EXPLANATION_MAX_OUTPUT_TOKENS",
            "2200"
        )
    )
except (TypeError, ValueError):
    EXPLANATION_MAX_TOKENS = 2200


# =========================================================
# GROQ HELPERS
# =========================================================

def get_groq_api_key():
    return os.environ.get("GROQ_API_KEY", "").strip()


def generate_explanations(topic, questions, user_answers):
    """
    Generate explanations for incorrect/unanswered questions
    in one batched Groq request.

    Returns:
        {
            "0": "Explanation...",
            "3": "Explanation..."
        }

    Never raises. If Groq is unavailable, the exam result
    is still saved without explanations.
    """

    api_key = get_groq_api_key()

    if not api_key:
        print(
            "[EXPLANATIONS] GROQ_API_KEY is missing",
            flush=True
        )
        return {}

    # -----------------------------------------------------
    # Collect only incorrect / unanswered questions
    # -----------------------------------------------------

    wrong_items = []

    for i, question in enumerate(questions):

        user_answer = user_answers.get(str(i))
        correct_answer = question.get("answer")

        # Correct -> no explanation needed
        if user_answer == correct_answer:
            continue

        wrong_items.append({
            "index": i,
            "question": question.get("question", ""),
            "options": question.get("options", {}),
            "correct_answer": correct_answer,
            "user_answer": user_answer,
        })

    if not wrong_items:
        return {}

    # -----------------------------------------------------
    # Prompt
    # -----------------------------------------------------

    prompt = f"""
You are a precise technical tutor helping a student learn from
mistakes in an MCQ test.

Topic:
{topic}

For each incorrect or unanswered question below, explain WHY
the correct answer is correct.

Use ONLY the question, options, and answer supplied in the data
as the source of truth.

Requirements for every explanation:

1. Explain the underlying concept clearly.
2. Explicitly explain why the correct option is correct.
3. If the student selected an answer, briefly explain why it is wrong.
4. Keep the explanation to 2-5 useful sentences.
5. For formulas, calculations, code, algorithms, SQL, mathematics,
   or technical concepts, explain the actual reasoning.
6. Do not give generic statements such as "this is correct".
7. Do not mention AI, Groq, prompts, or these instructions.
8. Do not invent information unrelated to the question.

Return ONLY valid JSON in exactly this format:

{{
  "explanations": {{
    "0": "Explanation for question 0",
    "2": "Explanation for question 2"
  }}
}}

Questions:

{json.dumps(wrong_items, ensure_ascii=False)}
"""

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return only valid JSON. "
                    "Be technically accurate and concise."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.15,
        "max_completion_tokens": EXPLANATION_MAX_TOKENS,
        "response_format": {
            "type": "json_object"
        },
        "stream": False
    }

    try:

        print(
            f"[EXPLANATIONS] Generating explanations for "
            f"{len(wrong_items)} question(s)...",
            flush=True
        )

        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )

        # -------------------------------------------------
        # Provider error
        # -------------------------------------------------

        if not response.ok:

            print(
                "[EXPLANATIONS] Groq HTTP error:",
                response.status_code,
                response.text[:500],
                flush=True
            )

            return {}

        data = response.json()

        content = (
            data
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )

        if not content:
            print(
                "[EXPLANATIONS] Groq returned empty content",
                flush=True
            )
            return {}

        parsed = json.loads(content)

        explanations = parsed.get(
            "explanations",
            {}
        )

        if not isinstance(explanations, dict):
            print(
                "[EXPLANATIONS] Invalid explanations object",
                flush=True
            )
            return {}

        # -------------------------------------------------
        # Only accept indexes we actually requested
        # -------------------------------------------------

        valid_indexes = {
            str(item["index"])
            for item in wrong_items
        }

        cleaned = {}

        for key, value in explanations.items():

            key = str(key)

            if key not in valid_indexes:
                continue

            if not isinstance(value, str):
                continue

            value = value.strip()

            if not value:
                continue

            cleaned[key] = value

        print(
            f"[EXPLANATIONS] Received {len(cleaned)} explanation(s)",
            flush=True
        )

        return cleaned

    except requests.RequestException as exc:

        print(
            "[EXPLANATIONS] Request failed:",
            repr(exc),
            flush=True
        )

        return {}

    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:

        print(
            "[EXPLANATIONS] Response parsing failed:",
            repr(exc),
            flush=True
        )

        return {}


# =========================================================
# BUILD DETAILED RESULT
# =========================================================

def build_detailed(
    questions,
    user_answers,
    explanations=None
):
    """
    Build the complete question-by-question result.

    Explanations are included ONLY for incorrect or
    unanswered questions.
    """

    explanations = explanations or {}

    detailed = []

    for i, question in enumerate(questions):

        user_answer = user_answers.get(
            str(i),
            None
        )

        correct_answer = question["answer"]

        is_correct = (
            user_answer == correct_answer
        )

        item = {
            "question": question["question"],
            "options": question["options"],
            "correct_answer": correct_answer,
            "user_answer": user_answer,
            "is_correct": is_correct
        }

        # -------------------------------------------------
        # Explanation only when wrong/unanswered
        # -------------------------------------------------

        if not is_correct:
            item["explanation"] = explanations.get(
                str(i),
                ""
            )

        detailed.append(item)

    return detailed


# =========================================================
# SUBMIT RESULT
# =========================================================

@results_bp.route("/submit", methods=["POST"])
def submit_result():

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "error": "Not authenticated"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    mock_id = data.get("mock_id")

    user_answers = data.get(
        "answers",
        {}
    )

    time_taken = data.get(
        "time_taken",
        0
    )

    # -----------------------------------------------------
    # Validate time
    # -----------------------------------------------------

    try:
        time_taken = max(
            0,
            int(time_taken)
        )
    except (TypeError, ValueError):
        time_taken = 0

    # -----------------------------------------------------
    # Validate answers
    # -----------------------------------------------------

    if not isinstance(user_answers, dict):

        return jsonify({
            "error": "Invalid answers payload"
        }), 400

    # -----------------------------------------------------
    # Find mock
    # -----------------------------------------------------

    mock = Mock.query.get(mock_id)

    if not mock or mock.user_id != user_id:

        return jsonify({
            "error": "Mock not found"
        }), 404

    # -----------------------------------------------------
    # Parse questions
    # -----------------------------------------------------

    try:

        questions = json.loads(
            mock.questions
        )

    except (
        TypeError,
        ValueError,
        json.JSONDecodeError
    ):

        return jsonify({
            "error": "Mock data is invalid"
        }), 500

    if not isinstance(questions, list) or not questions:

        return jsonify({
            "error": "Mock contains no questions"
        }), 500

    # -----------------------------------------------------
    # Calculate score
    # -----------------------------------------------------

    correct = 0
    wrong = 0

    for i, question in enumerate(questions):

        user_answer = user_answers.get(
            str(i)
        )

        correct_answer = question.get(
            "answer"
        )

        if user_answer == correct_answer:
            correct += 1
        else:
            wrong += 1

    # -----------------------------------------------------
    # Generate explanations in ONE Groq request
    # -----------------------------------------------------

    explanations = generate_explanations(
        mock.topic,
        questions,
        user_answers
    )

    # -----------------------------------------------------
    # Save result
    # -----------------------------------------------------

    result = Result(
        user_id=user_id,
        mock_id=mock_id,
        score=correct,
        total=len(questions),
        correct_answers=correct,
        wrong_answers=wrong,
        user_answers=json.dumps(
            user_answers
        ),
        explanations=json.dumps(
            explanations
        ),
        time_taken=time_taken
    )

    db.session.add(result)
    db.session.commit()

    # -----------------------------------------------------
    # Detailed result
    # -----------------------------------------------------

    detailed = build_detailed(
        questions,
        user_answers,
        explanations
    )

    return jsonify({
        "result_id": result.id,
        "score": correct,
        "total": len(questions),
        "percentage": round(
            (correct / len(questions)) * 100,
            1
        ),
        "correct_answers": correct,
        "wrong_answers": wrong,
        "time_taken": time_taken,
        "detailed": detailed,
        "topic": mock.topic
    }), 201


# =========================================================
# GET SINGLE RESULT
# =========================================================

# IMPORTANT:
# This MUST contain <int:result_id>.
# Your old version used '/' here while the function expected
# result_id, which breaks /api/results/<id>.
# =========================================================

@results_bp.route("/<int:result_id>", methods=["GET"])
def get_result(result_id):

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "error": "Not authenticated"
        }), 401

    result = Result.query.get(
        result_id
    )

    if not result or result.user_id != user_id:

        return jsonify({
            "error": "Result not found"
        }), 404

    mock = Mock.query.get(
        result.mock_id
    )

    if not mock:

        return jsonify({
            "error": "Mock not found"
        }), 404

    try:

        questions = json.loads(
            mock.questions
        )

        user_answers = json.loads(
            result.user_answers
        )

        explanations = json.loads(
            result.explanations or "{}"
        )

    except (
        TypeError,
        ValueError,
        json.JSONDecodeError
    ):

        return jsonify({
            "error": "Result data is invalid"
        }), 500

    detailed = build_detailed(
        questions,
        user_answers,
        explanations
    )

    return jsonify({
        "result_id": result.id,
        "score": result.score,
        "total": result.total,
        "percentage": round(
            (result.score / result.total) * 100,
            1
        ),
        "correct_answers": result.correct_answers,
        "wrong_answers": result.wrong_answers,
        "time_taken": result.time_taken,
        "timestamp": result.timestamp.isoformat(),
        "topic": mock.topic,
        "detailed": detailed
    }), 200


# =========================================================
# HISTORY
# =========================================================

@results_bp.route("/history", methods=["GET"])
def result_history():

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "error": "Not authenticated"
        }), 401

    results = (
        Result.query
        .filter_by(user_id=user_id)
        .order_by(Result.timestamp.desc())
        .limit(20)
        .all()
    )

    history = []

    for result in results:

        mock = Mock.query.get(
            result.mock_id
        )

        history.append({
            "result_id": result.id,
            "topic": (
                mock.topic
                if mock
                else "Unknown"
            ),
            "score": result.score,
            "total": result.total,
            "percentage": round(
                (result.score / result.total) * 100,
                1
            ),
            "timestamp": result.timestamp.isoformat()
        })

    return jsonify({
        "history": history
    }), 200