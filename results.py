from flask import Blueprint, request, jsonify, session
from database import db, User, Mock, Result

import json
import os
from groq_client import call_groq_http


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

def generate_explanations(topic, questions, user_answers):
    """
    Generate explanations for answered-but-incorrect questions
    in one batched Groq request.

    Returns:
        {
            "0": "Explanation...",
            "3": "Explanation..."
        }

    Never raises. If Groq is unavailable, the exam result
    is still saved without explanations.
    """

    # Groq authentication and API-key failover are handled centrally by
    # groq_client.call_groq_http(). Do not read a single GROQ_API_KEY here.

    # -----------------------------------------------------
    # Collect only incorrect / unanswered questions
    # -----------------------------------------------------

    wrong_items = []

    for i, question in enumerate(questions):

        user_answer = user_answers.get(str(i))
        correct_answer = question.get("answer")

        # Only answered-but-incorrect questions receive explanations.
        if user_answer is None or user_answer == "" or user_answer == correct_answer:
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

For each answered-but-incorrect question below, explain WHY
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

        data = call_groq_http(payload)

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

    except Exception as exc:

        print(
            "[EXPLANATIONS] Groq request failed:",
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

    Explanations are included ONLY for answered-but-incorrect questions.
    """

    explanations = explanations or {}

    detailed = []

    for i, question in enumerate(questions):

        user_answer = user_answers.get(
            str(i),
            None
        )

        correct_answer = question["answer"]

        is_unanswered = user_answer is None or user_answer == ""
        is_correct = (
            not is_unanswered and user_answer == correct_answer
        )
        is_wrong = not is_unanswered and not is_correct

        item = {
            "question": question["question"],
            "options": question["options"],
            "correct_answer": correct_answer,
            "user_answer": user_answer,
            "is_correct": is_correct,
            "is_wrong": is_wrong,
            "is_unanswered": is_unanswered
        }

        # Explanation only for answered-but-incorrect questions.
        if is_wrong:
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
    unanswered = 0

    for i, question in enumerate(questions):

        user_answer = user_answers.get(str(i))
        correct_answer = question.get("answer")

        if user_answer is None or user_answer == "":
            unanswered += 1
        elif user_answer == correct_answer:
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
        "unanswered_answers": unanswered,
        "answered_answers": correct + wrong,
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

    unanswered = max(
        0,
        result.total - result.correct_answers - result.wrong_answers
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
        "unanswered_answers": unanswered,
        "answered_answers": result.correct_answers + result.wrong_answers,
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