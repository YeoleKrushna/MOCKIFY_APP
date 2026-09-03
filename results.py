from flask import Blueprint, request, jsonify, session
from database import db, User, Mock, Result

import json
import os
import re
from groq_client import call_groq_http
from sarvam_client import SarvamAPIError, call_sarvam_http


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

def generate_explanations(topic, questions, user_answers, language="English"):
    """
    Generate explanations for answered-but-incorrect questions.

    English continues to use the existing Groq explanation path.
    Hindi/Marathi use Sarvam so the explanation stays in the selected language.
    """
    wrong_items = []

    for i, question in enumerate(questions):
        user_answer = user_answers.get(str(i))
        correct_answer = question.get("answer")

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

    is_sarvam = language in ("Hindi", "Marathi")

    if is_sarvam:
        language_name = "Hindi" if language == "Hindi" else "Marathi"

        prompt = f"""
You are a precise {language_name}-language tutor helping a student learn from mistakes in an MCQ test.

Topic:
{topic}

For each answered-but-incorrect question below, explain WHY the correct answer is correct.

LANGUAGE:
- Write every explanation completely in {language_name}.
- Use natural, standard {language_name} suitable for competitive-exam preparation.
- Do not switch to English.
- English technical/proper terms may remain only when they are standard terminology.

CONTENT:
- Use ONLY the supplied question, options, correct answer, and user answer as the source of truth.
- Explain the underlying concept clearly.
- Explain why the correct option is correct.
- Briefly explain why the student's selected answer is wrong.
- Keep each explanation to 2-5 useful sentences.
- Do not invent facts or add unrelated information.

Return ONLY valid JSON in exactly this format:
{{
  "explanations": {{
    "0": "Explanation in {language_name}"
  }}
}}

Questions:
{json.dumps(wrong_items, ensure_ascii=False)}
"""

        payload = {
            "model": "sarvam-105b",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"You are a precise {language_name} tutor. "
                        "Return only the requested JSON object. "
                        f"Every explanation must be written in {language_name}."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0.15,
            "max_tokens": int(os.environ.get("SARVAM_EXPLANATION_MAX_OUTPUT_TOKENS", "2200")),
            "reasoning_effort": None,
            "stream": False,
        }

        try:
            print(
                f"[EXPLANATIONS] Generating {language_name} explanations "
                f"for {len(wrong_items)} question(s)...",
                flush=True,
            )

            data = call_sarvam_http(payload)
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )

            text = str(content or "").strip()

            if text.startswith("```"):
                match = re.fullmatch(
                    r"```(?:json)?\s*(.*?)\s*```",
                    text,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if match:
                    text = match.group(1).strip()

            parsed = json.loads(text)
            explanations = parsed.get("explanations", {})

            if not isinstance(explanations, dict):
                return {}

            valid_indexes = {
                str(item["index"])
                for item in wrong_items
            }

            return {
                str(key): value.strip()
                for key, value in explanations.items()
                if str(key) in valid_indexes
                and isinstance(value, str)
                and value.strip()
            }

        except Exception as exc:
            print(
                "[EXPLANATIONS] Sarvam explanation request failed:",
                repr(exc),
                flush=True,
            )
            return {}

    # -----------------------------------------------------
    # Existing English/Groq explanation path
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

        # Some Groq models reject enforced JSON mode even when the prompt
        # requests JSON. Ask normally, then safely extract the JSON locally.
        text = str(content).strip()

        if text.startswith("```"):
            match = re.fullmatch(
                r"```(?:json)?\s*(.*?)\s*```",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if match:
                text = match.group(1).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            if start < 0:
                raise
            parsed, _ = json.JSONDecoder().raw_decode(text[start:])

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
    # One submission per mock
    # -----------------------------------------------------
    # Mobile browsers can retry a POST or restore an old exam page.
    # Treat a previously completed mock as immutable and return the
    # original result instead of creating another attempt.
    existing_result = (
        Result.query
        .filter_by(user_id=user_id, mock_id=mock_id)
        .order_by(Result.timestamp.desc())
        .first()
    )

    if existing_result:
        try:
            existing_answers = json.loads(existing_result.user_answers)
            existing_explanations = json.loads(
                existing_result.explanations or "{}"
            )
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return jsonify({
                "error": "Existing result data is invalid"
            }), 500

        existing_unanswered = max(
            0,
            existing_result.total
            - existing_result.correct_answers
            - existing_result.wrong_answers
        )

        detailed = build_detailed(
            questions,
            existing_answers,
            existing_explanations,
        )

        return jsonify({
            "result_id": existing_result.id,
            "score": existing_result.score,
            "total": existing_result.total,
            "percentage": round(
                (existing_result.score / existing_result.total) * 100,
                1
            ),
            "correct_answers": existing_result.correct_answers,
            "wrong_answers": existing_result.wrong_answers,
            "unanswered_answers": existing_unanswered,
            "answered_answers": (
                existing_result.correct_answers
                + existing_result.wrong_answers
            ),
            "time_taken": existing_result.time_taken,
            "detailed": detailed,
            "topic": mock.topic,
            "already_submitted": True,
        }), 200

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
    # Generate explanations in the language stored with this mock.
    # The session may be missing or may reflect a different mock generated
    # later in the same browser session.
    # -----------------------------------------------------

    language = str(
        getattr(mock, "language", "English") or "English"
    ).strip().title()

    if language not in ("English", "Hindi", "Marathi"):
        language = "English"

    explanations = generate_explanations(
        mock.topic,
        questions,
        user_answers,
        language=language,
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


@results_bp.route("/<int:result_id>", methods=["DELETE"])
def delete_result(result_id):
    """Allow a learner to remove only their own result history entry."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401

    result = db.session.get(Result, result_id)
    if not result or result.user_id != user_id:
        return jsonify({"error": "Result not found"}), 404

    db.session.delete(result)
    db.session.commit()
    return jsonify({"message": "Result removed from your history"}), 200


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

    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(50, max(1, int(request.args.get("per_page", 12))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid history pagination."}), 400

    search = str(request.args.get("q", "")).strip()[:120]
    topic = str(request.args.get("topic", "")).strip()[:500]
    score_band = str(request.args.get("score", "all")).strip().lower()
    sort = str(request.args.get("sort", "recent")).strip().lower()

    query = Result.query.join(Mock, Result.mock_id == Mock.id).filter(
        Result.user_id == user_id
    )
    if search:
        query = query.filter(Mock.topic.ilike(f"%{search}%"))
    if topic:
        query = query.filter(Mock.topic == topic)
    if score_band == "strong":
        query = query.filter((Result.score * 100.0 / Result.total) >= 80)
    elif score_band == "steady":
        query = query.filter((Result.score * 100.0 / Result.total) >= 50, (Result.score * 100.0 / Result.total) < 80)
    elif score_band == "review":
        query = query.filter((Result.score * 100.0 / Result.total) < 50)

    if sort == "score_high":
        query = query.order_by(Result.score.desc(), Result.timestamp.desc())
    elif sort == "score_low":
        query = query.order_by(Result.score.asc(), Result.timestamp.desc())
    else:
        sort = "recent"
        query = query.order_by(Result.timestamp.desc())

    total = query.count()
    results = query.offset((page - 1) * per_page).limit(per_page).all()
    best_score = (
        db.session.query(db.func.max(Result.score))
        .filter(Result.user_id == user_id)
        .scalar()
    )

    history = []

    for result in results:

        history.append({
            "result_id": result.id,
            "topic": result.mock.topic,
            "score": result.score,
            "total": result.total,
            "percentage": round(
                (result.score / result.total) * 100,
                1
            ),
            "timestamp": result.timestamp.isoformat()
        })

    topics = [row[0] for row in (
        db.session.query(Mock.topic)
        .join(Result, Result.mock_id == Mock.id)
        .filter(Result.user_id == user_id)
        .distinct()
        .order_by(Mock.topic.asc())
        .all()
    )]

    return jsonify({
        "history": history,
        "topics": topics,
        "page": page,
        "per_page": per_page,
        "total": total,
        "best_score": best_score,
        "has_more": page * per_page < total,
        "sort": sort,
    }), 200
