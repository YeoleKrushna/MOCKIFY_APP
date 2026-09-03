from flask import Blueprint, request, jsonify, session
from database import db, User, Mock, Result

import difflib
import json
import os
import random
import re
import time
from datetime import datetime

from dotenv import load_dotenv

from groq_client import GroqAPIError, call_groq_http
from sarvam_client import SarvamAPIError, call_sarvam_http


mock_bp = Blueprint("mock", __name__)

load_dotenv(override=True)


# =========================================================
# CONFIG
# =========================================================

GROQ_MODEL = os.environ.get(
    "GROQ_MODEL",
    "openai/gpt-oss-20b",
).strip()

GROQ_MAX_OUTPUT_TOKENS = int(
    os.environ.get("GROQ_MAX_OUTPUT_TOKENS", "4000")
)

# More attempts are important because a generated set can still contain
# a question that appeared in an older mock. Those sets are rejected
# locally and regenerated.
GROQ_MAX_RETRIES = max(
    3,
    int(os.environ.get("GROQ_MAX_RETRIES", "5"))
)

GROQ_RETRY_BASE_DELAY = max(
    1,
    int(os.environ.get("GROQ_RETRY_BASE_DELAY", "2"))
)

SARVAM_MAX_RETRIES = max(
    2,
    min(3, int(os.environ.get("SARVAM_MAX_RETRIES", "2")))
)

SARVAM_RETRY_BASE_DELAY = max(
    0,
    int(os.environ.get("SARVAM_RETRY_BASE_DELAY", "1"))
)

GENERATE_COOLDOWN_SECONDS = max(
    1,
    int(os.environ.get("GENERATE_COOLDOWN_SECONDS", "15"))
)

# No same-topic cache.
# Every request that passes the daily limit creates a new mock.
last_generate_attempts: dict[int, float] = {}


# =========================================================
# PROMPT
# =========================================================

MOCK_PROMPT = """You are an expert exam and interview question designer.

Requested topic:
{user_prompt}

Requested language:
{language}

LANGUAGE REQUIREMENTS:
- Generate the question, all four options, and the answer explanation in the requested language.
- For English, use the normal existing Mockify English generation style.
- For Hindi, use natural, standard Hindi suitable for Indian competitive and government examinations.
- For Marathi, use natural, standard Marathi suitable for MPSC, Talathi, police recruitment, banking, and other Indian competitive examinations.
- Avoid awkward literal translations and unnecessary code-mixing. Keep widely used technical/proper terms in English only when that is standard in the target exam context.
- Do not translate names of laws, technologies, APIs, programming keywords, or established technical terms unnaturally.
- Preserve factual precision. Never invent facts, case names, article numbers, dates, schemes, people, or terminology.

The learner has already taken previous mock tests on this same topic.
You MUST create a genuinely NEW 10-question mock.

HIGHEST PRIORITY — DO NOT REUSE OLD QUESTIONS:
- Do not repeat any previous question.
- Do not lightly reword, paraphrase, or rearrange an old question.
- Do not reuse the same scenario, code example, numerical values, names,
  or exact reasoning pattern from a previous question.
- Even when testing the same concept, use a materially different question
  angle, scenario, example, or application.
- Prefer subtopics and dimensions that were not emphasized in the previous
  questions.
- If a previous question tests "what X is", test a different aspect such as
  application, comparison, debugging, output prediction, trade-off, edge
  case, or scenario reasoning instead.
- The previous questions below are an exclusion list, not inspiration.

Create ONE high-quality 10-question MCQ mock test that gives the learner broad
and useful coverage of the requested topic.

Before writing the questions, silently identify important subtopics and
dimensions of the requested topic. Distribute the 10 questions across those
dimensions so the test is as comprehensive as possible.

Coverage priorities (adapt to the topic; do not force irrelevant categories):
1. Core definition, purpose, and fundamentals
2. Key concepts, components, terminology, or structure
3. How the topic works / mechanism / workflow / principles
4. Practical application or implementation
5. Syntax, formulas, commands, APIs, configuration, or technical details
   when relevant
6. Comparison, trade-offs, or choosing between related concepts when relevant
7. Edge cases, constraints, limitations, or failure modes when relevant
8. Debugging, troubleshooting, output prediction, or scenario reasoning
   when relevant
9. Common mistakes, misconceptions, or exam traps
10. Advanced/interview-level understanding or real-world decision making

Question-quality requirements:
- Exactly 10 questions.
- Every question must be directly about the requested topic.
- Questions must cover different subtopics or reasoning angles.
- Prefer application, reasoning, scenario, comparison, debugging, and
  output-prediction questions over trivial memorization when the topic allows.
- Difficulty should be mixed: foundational, moderate, and a few challenging.
- For narrow topics, stay relevant while varying the angle.
- Every option must be plausible enough to require understanding.
- Exactly one option must be correct.
- Do not make the correct answer consistently longer than the distractors.
- Do not make the correct answer follow a visible pattern.
- Do not use the same correct-answer letter for multiple questions in a row.
- Keep questions and options reasonably concise.
- When a question, option, or example contains source code, ALWAYS put the
  complete code inside a fenced code block using triple backticks.
- You may include a language identifier after the opening backticks when useful
  (for example ```python, ```javascript, ```sql, or ```java).
- Preserve code indentation, line breaks, symbols, and punctuation exactly.
- Never use HTML tags to format code.

IMPORTANT:
The application will independently validate the generated set against
previous questions and may reject it if it contains duplicates.

Previous questions from earlier mocks on this topic:
{previous_questions}

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


# =========================================================
# TOPIC / QUESTION NORMALIZATION
# =========================================================

def normalize_topic(topic: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(topic)).strip().lower()
    return cleaned[:120]


def normalize_question_text(question: str) -> str:
    text = str(question or "").lower().strip()

    # Remove punctuation/formatting noise so exact duplicates with tiny
    # punctuation differences are still detected.
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    # Preserve Unicode letters/digits (including Devanagari) so duplicate
    # detection works for Hindi/Marathi as well as English.
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def _word_jaccard(a: str, b: str) -> float:
    a_tokens = set(normalize_question_text(a).split())
    b_tokens = set(normalize_question_text(b).split())
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def _char_ngram_jaccard(a: str, b: str, n: int = 3) -> float:
    a_text = normalize_question_text(a).replace(" ", "")
    b_text = normalize_question_text(b).replace(" ", "")
    if len(a_text) < n or len(b_text) < n:
        return 0.0
    a_grams = {a_text[i:i+n] for i in range(len(a_text)-n+1)}
    b_grams = {b_text[i:i+n] for i in range(len(b_text)-n+1)}
    if not a_grams or not b_grams:
        return 0.0
    return len(a_grams & b_grams) / len(a_grams | b_grams)


def question_similarity(a: str, b: str) -> float:
    """Cheap multilingual similarity for exact and paraphrased duplicates."""
    return max(
        _word_jaccard(a, b),
        _char_ngram_jaccard(a, b),
        difflib.SequenceMatcher(
            None,
            normalize_question_text(a),
            normalize_question_text(b),
        ).ratio(),
    )


# =========================================================
# DAILY GENERATION COOLDOWN
# =========================================================

def get_generate_cooldown_remaining(user_id: int) -> int:
    now = time.time()
    last_request_time = last_generate_attempts.get(user_id)

    if last_request_time is None:
        return 0

    remaining = GENERATE_COOLDOWN_SECONDS - (
        now - last_request_time
    )

    if remaining > 0:
        return (
            int(remaining)
            if remaining.is_integer()
            else int(remaining) + 1
        )

    return 0


def mark_generate_attempt(user_id: int) -> None:
    last_generate_attempts[user_id] = time.time()


# =========================================================
# PREVIOUS QUESTION RETRIEVAL
# =========================================================

def get_previous_questions(
    user_id: int,
    topic: str,
) -> list[str]:
    """
    Load all previously generated question texts for this exact user/topic.

    The database already stores the complete question JSON in Mock.questions,
    so no schema change is required.
    """
    normalized_topic = normalize_topic(topic)

    mocks = (
        Mock.query
        .filter_by(user_id=user_id)
        .order_by(Mock.created_at.desc())
        .all()
    )

    previous: list[str] = []
    seen: set[str] = set()

    for mock in mocks:
        if normalize_topic(mock.topic) != normalized_topic:
            continue

        try:
            questions = json.loads(mock.questions)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

        if not isinstance(questions, list):
            continue

        for question in questions:
            if not isinstance(question, dict):
                continue

            text = str(question.get("question", "")).strip()
            normalized = normalize_question_text(text)

            if normalized and normalized not in seen:
                seen.add(normalized)
                previous.append(text)

    return previous


def build_previous_question_block(
    previous_questions: list[str],
    *,
    max_items: int = 100,
) -> str:
    """
    Give Groq a recent/representative exclusion list.

    Local validation below still checks ALL historical questions, so the
    prompt size limit does not weaken duplicate protection.
    """
    if not previous_questions:
        return "No previous questions exist. This is the learner's first mock on this topic."

    selected = previous_questions[:max_items]

    return "\n".join(
        f"{index + 1}. {question}"
        for index, question in enumerate(selected)
    )


# =========================================================
# GROQ
# =========================================================

def extract_groq_content(data: dict) -> str:
    content = (
        data
        .get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []

        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
            ):
                parts.append(str(item.get("text", "")))

        return "".join(parts)

    raise ValueError("Groq returned unsupported message content.")


def call_groq_once(prompt: str) -> dict:
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise MCQ generator. "
                    "Return exactly the requested JSON object."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.65,
        "max_completion_tokens": GROQ_MAX_OUTPUT_TOKENS,
        "stream": False,
    }

    data = call_groq_http(payload)

    try:
        content = extract_groq_content(data)
        # Groq is prompted for raw JSON but may still wrap it in a JSON fence
        # or brief surrounding text. Normalize that safely before local
        # parsing instead of relying on provider-side JSON mode.
        return json.loads(_extract_json_text(content))
    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise GroqAPIError(
            "AI returned an unexpected JSON response. Please try again.",
            # This is a model-output validation failure, not an outage.  It
            # is safe to retry the English/Groq generation request.
            status_code=422,
            retryable=True,
        ) from exc


def _extract_json_text(content: str) -> str:
    """Normalize Sarvam JSON output, including optional markdown fences."""
    text = str(content or "").strip()

    if not text:
        raise ValueError("Sarvam returned an empty response.")

    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        text = fenced.group(1).strip()

    # Be tolerant if the model adds a tiny amount of text around the JSON.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

    return text



SARVAM_MOCK_PROMPT = """You are an expert exam question designer creating a fresh Mockify mock test.

Requested topic:
{user_prompt}

Requested language:
{language}

IMPORTANT — GENERATION TARGET:
Generate EXACTLY 12 candidate multiple-choice questions in this response.
The application will validate these 12 candidates and select the best 10 valid questions.
Therefore, every one of the 12 candidates must be complete and high quality.

TOPIC INTERPRETATION — HIGHEST PRIORITY:
- The requested topic may be typed in English, Hindi, Marathi, Romanized Hindi, Romanized Marathi, or a mixture.
- Before generating anything, silently understand the COMPLETE semantic meaning of the user's entire topic in the selected language.
- Do NOT treat the raw topic string as if it were already correctly written in the selected language.
- The exam name/context is part of the topic and must be understood.
- Example: "bhugol talathi exam" with Marathi selected MUST be understood as "भूगोल तलाठी परीक्षा".
- Example: "marathi vyakaran for talathi exam" with Marathi selected MUST be understood as "मराठी व्याकरण तलाठी परीक्षा".
- Example: "modern history upsc prelims" with Hindi selected means आधुनिक भारतीय इतिहास in the UPSC प्रारंभिक परीक्षा context.
- Do NOT generate questions about the literal English/Romanized wording itself.
- Do not merely translate individual words; understand the full phrase and its intended exam context first.

STRICT SELECTED-LANGUAGE RULE:
- The question MUST be written in {language}.
- ALL four options MUST be written in {language}.
- The explanation MUST be written in {language}.
- The explanation is especially strict: NEVER write the explanation in English.
- Do not write an English sentence followed by a translated sentence.
- Do not use English as the main language of any explanation.
- Use the native script appropriate to {language}.
- For Hindi, use natural, standard Hindi suitable for Indian competitive/government examinations.
- For Marathi, use natural, standard Marathi suitable for MPSC, Talathi, police recruitment, banking, and other competitive examinations.
- Avoid unnecessary code-mixing. Widely accepted technical/proper terms may remain in English only when genuinely standard in the selected exam context.
- Never use Romanized Hindi/Marathi when native script is appropriate.
- Never invent facts, article numbers, dates, case names, schemes, laws, people, terminology, or examples.

QUESTION REQUIREMENTS:
- Generate EXACTLY 12 complete candidate questions.
- Each question must be directly about the fully understood requested topic.
- Cover different subtopics, facts, concepts, applications, comparisons, statement-based reasoning, exam traps, and harder angles where relevant.
- Do not generate 12 variations of the same question.
- Questions should be suitable for the requested exam/context.
- Exactly ONE correct answer per question.

CRITICAL OPTION UNIQUENESS RULE:
For EVERY question, A, B, C, and D MUST be four genuinely different answer choices.
- NEVER repeat the same option.
- NEVER copy one option into another.
- NEVER use minor spelling, punctuation, spacing, or formatting changes to disguise a duplicate.
- NEVER use the same answer text twice within one question.
- The four choices must be meaningfully distinct, not merely visually different.

Before returning EACH question, silently verify:
A != B
A != C
A != D
B != C
B != D
C != D

CORRECT-ANSWER RULE:
- The value of "answer" MUST be exactly one of A, B, C, or D.
- The answer must point to the correct option content.
- Exactly one option must be correct.
- Do not make the correct option systematically longer than the distractors.
- Vary the correct-answer positions naturally; the application may rebalance positions later.

EXPLANATION REQUIREMENT:
- EVERY question MUST contain an "explanation" field.
- The explanation MUST directly explain why the selected answer is correct.
- The explanation MUST be in {language}.
- For Marathi, write the explanation as natural Marathi, not English.
- For Hindi, write the explanation as natural Hindi, not English.
- Do not leave explanations empty or use generic filler.

QUALITY AND COMPLETENESS:
- Do not leave any question, option, answer, or explanation empty.
- Do not use placeholder text.
- Avoid ambiguous questions.
- Avoid questions with two potentially correct options.
- Avoid duplicate or near-duplicate questions within the 12 candidates.
- If a candidate is weak, replace it with a stronger question before returning the response.

FINAL SELF-CHECK BEFORE RESPONSE:
1. There are EXACTLY 12 questions.
2. Every question has A, B, C, D.
3. A/B/C/D are all different for every question.
4. Every question has exactly one correct answer.
5. Every question has a non-empty explanation.
6. Questions, options, and explanations are in the selected language {language}.
7. Explanations are NOT in English.
8. The 12 questions cover different useful angles.
9. No obvious duplicate questions exist.
10. The JSON is complete and valid.

Return ONLY valid JSON. No markdown fences. No commentary before or after the JSON.

Output exactly this structure:
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
      "answer": "A",
      "explanation": "..."
    }}
  ]
}}
"""



SARVAM_QUALITY_PROMPT = """
SARVAM-SPECIFIC QUESTION DESIGN:
- Build a deliberate difficulty progression: foundational questions first, then moderate conceptual/application questions, then a few challenging questions.
- Do not make all questions simple definitions or direct recall.
- Vary the angle across the set: fundamentals, concepts, mechanism/workflow, application, comparison, scenario/case, edge case or limitation, misconception/exam trap, and advanced reasoning when relevant.
- For government-exam topics, prefer genuine exam-style factual, statement-based, comparison, application, elimination, and scenario questions. Never invent article numbers, dates, case names, schemes, people, terminology, or facts.
- For programming/technical topics, mix conceptual, code/output, debugging, implementation, trade-off, edge-case, and practical scenario questions where relevant.
- Use natural, standard {language} terminology. The question, all four options, and every explanation must be in {language}; do not switch the explanation to English.
- BEFORE generating, silently convert/understand the COMPLETE requested topic in {language}. The input may be Romanized, English, Hindi, Marathi, or mixed-language.
- Do not treat the raw topic string as the final topic. Understand its complete semantic meaning, including the exam/context.
- For Marathi, "bhugol talathi exam" MUST be understood as "भूगोल तलाठी परीक्षा", not as the literal Romanized phrase. "marathi vyakaran for talathi exam" MUST be understood as "मराठी व्याकरण तलाठी परीक्षा".
- For Hindi, Romanized or mixed topics must likewise be understood as their complete natural Hindi meaning before generating.
- Generate the questions, options, and explanations from that understood topic.
- Do not generate questions about the literal English/Romanized wording itself.
- For Marathi, the explanation must be written as a Marathi sentence/paragraph. English technical terms may appear only inside an otherwise Marathi explanation.
- For Hindi, the explanation must be written as a Hindi sentence/paragraph. English technical terms may appear only when they are standard terminology.
- Correct-answer options may naturally be short, medium, or long. Vary option lengths across the four choices; never make the correct option systematically longer or more detailed than the distractors.
- Do not make all correct answers appear to be option A. The application will rebalance answer positions after validation.
- Do not repeat or lightly paraphrase anything from the exclusion list.
- Return exactly 10 complete questions.
"""


def call_sarvam_once(prompt: str) -> dict:
    payload = {
        "model": "sarvam-105b",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise competitive-exam MCQ generator. "
                    "Return exactly the requested JSON object and never include "
                    "markdown fences or commentary."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.4,
        "max_tokens": int(os.environ.get("SARVAM_MAX_OUTPUT_TOKENS", "5000")),
        "reasoning_effort": None,
        "stream": False,
    }

    data = call_sarvam_http(payload)

    try:
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "")

        # Some provider variants can expose text as a content-part list.
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )

        normalized = _extract_json_text(content)
        return json.loads(normalized)
    except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise SarvamAPIError(
            "Sarvam returned an unexpected JSON response. Please try again.",
            status_code=502,
            retryable=True,
        ) from exc


# =========================================================
# QUESTION VALIDATION
# =========================================================

def _normalize_option_text(value: object) -> str:
    """Normalize option text for duplicate detection without losing Unicode."""
    text = str(value or "").strip().lower()
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _has_devanagari(text: object) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", str(text or "")))


def _looks_like_english_sentence(text: object) -> bool:
    """Catch an English explanation while allowing occasional standard terms."""
    value = str(text or "").strip()
    latin_words = re.findall(r"[A-Za-z]{2,}", value)
    devanagari_chars = re.findall(r"[\u0900-\u097F]", value)

    if not latin_words:
        return False

    # A predominantly Latin explanation is not acceptable for Hindi/Marathi.
    if len(devanagari_chars) == 0:
        return True

    return len(latin_words) >= 6 and len(latin_words) > len(devanagari_chars) / 2


def _valid_sarvam_localized_text(
    text: object,
    *,
    explanation: bool = False,
    require_devanagari: bool = True,
) -> bool:
    value = str(text or "").strip()

    if not value:
        return False

    # Hindi and Marathi both use Devanagari. Questions and explanations must
    # use the native script. Options are allowed to contain legitimate
    # acronyms/proper/technical terms, so they are checked less aggressively.
    if require_devanagari and not _has_devanagari(value):
        return False

    if explanation and _looks_like_english_sentence(value):
        return False

    return True


def get_valid_sarvam_questions(questions_data: object) -> list[dict]:
    """
    Validate Sarvam's 12 candidates and return valid unique candidates.

    This is intentionally separate from validate_basic_questions() so the
    existing English/Groq validation path remains unchanged.
    """
    if not isinstance(questions_data, dict):
        return []

    questions = questions_data.get("questions", [])
    if not isinstance(questions, list):
        return []

    valid: list[dict] = []
    seen_questions: list[str] = []

    for question in questions:
        if not isinstance(question, dict):
            continue

        question_text = str(question.get("question", "")).strip()
        options = question.get("options")
        answer = question.get("answer")
        explanation = str(question.get("explanation", "")).strip()

        if not question_text or not isinstance(options, dict):
            continue

        if not all(key in options for key in ("A", "B", "C", "D")):
            continue

        option_values = {
            key: str(options.get(key, "")).strip()
            for key in ("A", "B", "C", "D")
        }

        if not all(option_values.values()):
            continue

        # Reject repeated/near-identical option content within one question.
        normalized_options = [
            _normalize_option_text(option_values[key])
            for key in ("A", "B", "C", "D")
        ]
        if any(not value for value in normalized_options):
            continue
        if len(set(normalized_options)) != 4:
            continue

        if answer not in ("A", "B", "C", "D"):
            continue

        if not explanation:
            continue

        if not _valid_sarvam_localized_text(question_text):
            continue

        if not all(
            _valid_sarvam_localized_text(
                option_values[key],
                require_devanagari=False,
            )
            for key in ("A", "B", "C", "D")
        ):
            continue

        if not _valid_sarvam_localized_text(
            explanation,
            explanation=True,
        ):
            continue

        normalized_question = normalize_question_text(question_text)
        if not normalized_question:
            continue

        # Reject exact and strong near-duplicate questions among the 12.
        if any(
            question_similarity(question_text, previous) >= 0.86
            for previous in seen_questions
        ):
            continue

        clean_question = {
            "question": question_text,
            "options": option_values,
            "answer": answer,
            "explanation": explanation,
        }

        valid.append(clean_question)
        seen_questions.append(question_text)

    return valid


def validate_basic_questions(questions_data) -> bool:
    if not isinstance(questions_data, dict):
        return False

    questions = questions_data.get("questions", [])

    if not isinstance(questions, list) or len(questions) != 10:
        return False

    seen: set[str] = set()

    for question in questions:
        if not isinstance(question, dict):
            return False

        if not all(
            key in question
            for key in ("question", "options", "answer")
        ):
            return False

        question_text = str(
            question.get("question", "")
        ).strip()

        if not question_text:
            return False

        normalized = normalize_question_text(question_text)

        if not normalized or normalized in seen:
            return False

        seen.add(normalized)

        options = question.get("options")

        if not isinstance(options, dict):
            return False

        if not all(
            key in options
            for key in ("A", "B", "C", "D")
        ):
            return False

        if not all(
            str(options[key]).strip()
            for key in ("A", "B", "C", "D")
        ):
            return False

        if question.get("answer") not in ("A", "B", "C", "D"):
            return False

    return True


def find_repeated_questions(
    questions_data: dict,
    previous_questions: list[str],
) -> list[str]:
    """Find exact/near duplicate questions against the full user/topic history."""
    generated = questions_data.get("questions", [])
    repeated: list[str] = []
    accepted: list[str] = []

    normalized_previous = {
        normalize_question_text(question): question
        for question in previous_questions
    }

    for question in generated:
        current = str(question.get("question", "")).strip()
        current_normalized = normalize_question_text(current)

        if not current_normalized:
            repeated.append(current)
            continue

        if current_normalized in normalized_previous:
            repeated.append(current)
            continue

        if any(
            question_similarity(current, old_question) >= 0.82
            for old_question in previous_questions
        ):
            repeated.append(current)
            continue

        if any(
            question_similarity(current, earlier) >= 0.86
            for earlier in accepted
        ):
            repeated.append(current)
            continue

        accepted.append(current)

    return repeated


def answer_letter_counts(questions: list[dict]) -> dict[str, int]:
    return {
        letter: sum(
            1
            for question in questions
            if question.get("answer") == letter
        )
        for letter in ("A", "B", "C", "D")
    }


# =========================================================
# ANSWER BALANCING + RANDOMIZATION
# =========================================================

def rebalance_answers(
    questions_data: dict,
) -> dict:
    """
    Reorder options so the correct-answer letters are balanced.

    Final distribution for 10 questions is exactly:
        A = 3
        B = 3
        C = 2
        D = 2

    The correct answer *content* is preserved; only option positions change.
    """
    questions = questions_data["questions"]

    target_letters = [
        "A", "A", "A",
        "B", "B", "B",
        "C", "C",
        "D", "D",
    ]

    random.SystemRandom().shuffle(questions)
    random.SystemRandom().shuffle(target_letters)

    for question, target_letter in zip(
        questions,
        target_letters,
    ):
        original_options = {
            key: str(question["options"][key])
            for key in ("A", "B", "C", "D")
        }

        old_answer = question["answer"]
        correct_text = original_options[old_answer]

        distractors = [
            original_options[key]
            for key in ("A", "B", "C", "D")
            if key != old_answer
        ]

        random.SystemRandom().shuffle(distractors)

        new_options = {
            "A": "",
            "B": "",
            "C": "",
            "D": "",
        }

        new_options[target_letter] = correct_text

        other_letters = [
            letter
            for letter in ("A", "B", "C", "D")
            if letter != target_letter
        ]

        for letter, distractor in zip(
            other_letters,
            distractors,
        ):
            new_options[letter] = distractor

        question["options"] = new_options
        question["answer"] = target_letter

    return {
        "questions": questions,
    }


# =========================================================
# GENERATION WITH HARD NO-REPEAT CHECK
# =========================================================

def generate_unique_mock(
    user_id: int,
    topic: str,
    language: str = "English",
) -> dict:
    """
    English keeps the existing historical no-repeat workflow.
    Hindi/Marathi use Sarvam to generate 12 candidates, validate them locally,
    keep exactly 10 valid candidates, and then use the existing answer-position
    balancing. English/Groq keeps its existing historical no-repeat workflow.
    """
    is_sarvam = language in ("Hindi", "Marathi")

    if is_sarvam:
        prompt = SARVAM_MOCK_PROMPT.format(
            user_prompt=topic,
            language=language,
        )

        print(
            f"[MOCK] Generating 12-question Sarvam candidate set | user={user_id} | "
            f"topic={topic!r} | language={language}",
            flush=True,
        )

        last_error: SarvamAPIError | None = None

        for attempt in range(1, SARVAM_MAX_RETRIES + 1):
            try:
                questions_data = call_sarvam_once(prompt)
            except (SarvamAPIError, ValueError) as exc:
                last_error = exc if isinstance(exc, SarvamAPIError) else SarvamAPIError(
                    str(exc),
                    status_code=502,
                    retryable=True,
                )

                print(
                    f"[MOCK] Sarvam generation failed on attempt "
                    f"{attempt}/{SARVAM_MAX_RETRIES} | language={language} | "
                    f"error={getattr(exc, 'message', str(exc))}",
                    flush=True,
                )
            else:
                valid_questions = get_valid_sarvam_questions(questions_data)

                print(
                    f"[MOCK] Sarvam candidate validation: "
                    f"{len(valid_questions)}/12 valid on attempt "
                    f"{attempt}/{SARVAM_MAX_RETRIES}.",
                    flush=True,
                )

                if len(valid_questions) >= 10:
                    # Select exactly 10 valid candidates. Keep the existing
                    # answer-position balancing unchanged.
                    selected = valid_questions[:10]
                    result = {"questions": selected}
                    return rebalance_answers(result)

                last_error = SarvamAPIError(
                    "Sarvam generated fewer than 10 valid questions. Please try again.",
                    status_code=422,
                    retryable=True,
                )

            if attempt < SARVAM_MAX_RETRIES:
                delay = SARVAM_RETRY_BASE_DELAY * attempt
                if delay > 0:
                    print(
                        f"[MOCK] Retrying Sarvam generation in {delay}s...",
                        flush=True,
                    )
                    time.sleep(delay)

        if last_error is not None:
            raise last_error

        raise SarvamAPIError(
            "Sarvam could not generate 10 valid questions. Please try again.",
            status_code=422,
            retryable=False,
        )

    # -----------------------------
    # Existing English/Groq path
    # -----------------------------
    previous_questions = get_previous_questions(
        user_id,
        topic,
    )

    previous_block = build_previous_question_block(
        previous_questions,
        max_items=100,
    )

    last_error: GroqAPIError | SarvamAPIError | None = None

    for attempt in range(1, GROQ_MAX_RETRIES + 1):
        extra_instruction = ""

        if attempt > 1:
            extra_instruction = """
A previous generation was rejected because it reused or closely resembled
questions from earlier mocks.

For this attempt:
- Change the subtopics materially.
- Avoid previously used concepts/angles where possible.
- Use fresh scenarios, examples, code, numbers, names, and reasoning paths.
- Do NOT simply reword an earlier question.
"""

        prompt = MOCK_PROMPT.format(
            user_prompt=topic,
            language=language,
            previous_questions=previous_block,
        ) + extra_instruction

        try:
            questions_data = call_groq_once(prompt)

            if not validate_basic_questions(questions_data):
                last_error = GroqAPIError(
                    "AI generated an incomplete or invalid 10-question mock.",
                    status_code=422,
                )
                print(
                    f"[MOCK] Invalid generation attempt "
                    f"{attempt}/{GROQ_MAX_RETRIES}.",
                    flush=True,
                )
            else:
                repeated = find_repeated_questions(
                    questions_data,
                    previous_questions,
                )

                if repeated:
                    last_error = GroqAPIError(
                        "AI repeated a previous question.",
                        status_code=422,
                    )

                    print(
                        f"[MOCK] Rejected {len(repeated)} repeated/"
                        "near-duplicate question(s) "
                        f"on attempt {attempt}/{GROQ_MAX_RETRIES}.",
                        flush=True,
                    )
                else:
                    questions_data = rebalance_answers(
                        questions_data
                    )

                    counts = answer_letter_counts(
                        questions_data["questions"]
                    )

                    if counts != {
                        "A": 3,
                        "B": 3,
                        "C": 2,
                        "D": 2,
                    }:
                        last_error = GroqAPIError(
                            "Generated answer distribution was invalid.",
                            status_code=422,
                        )
                    else:
                        return questions_data

        except GroqAPIError as exc:
            last_error = exc

            if exc.status_code not in (422, 429):
                raise

        if attempt < GROQ_MAX_RETRIES:
            delay = GROQ_RETRY_BASE_DELAY * attempt
            print(
                f"[MOCK] Retrying generation in {delay}s...",
                flush=True,
            )
            time.sleep(delay)

    if last_error is not None:
        raise last_error

    raise GroqAPIError(
        "AI could not generate a unique 10-question mock.",
        status_code=502,
    )


# =========================================================
# GENERATE
# =========================================================

@mock_bp.route("/generate", methods=["POST"])
def generate_mock():
    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "error": "Not authenticated",
        }), 401

    user = User.query.get(user_id)

    if not user:
        return jsonify({
            "error": "User not found",
        }), 404

    if not user.can_take_mock():
        return jsonify({
            "error": (
                f"Daily limit reached. "
                f"You can take {user.daily_mock_limit} "
                "mock(s) per day."
            ),
        }), 429

    data = request.get_json(silent=True) or {}

    topic = str(
        data.get("topic", "")
    ).strip()

    if not topic:
        return jsonify({
            "error": "Topic is required",
        }), 400

    if len(topic) > 500:
        return jsonify({
            "error": "Topic too long (max 500 chars)",
        }), 400

    language_raw = str(data.get("language", "English")).strip().lower()
    language_map = {
        "english": "English",
        "hindi": "Hindi",
        "marathi": "Marathi",
    }
    language = language_map.get(language_raw)

    if language is None:
        return jsonify({
            "error": "Please select a valid language: English, Hindi, or Marathi.",
        }), 400

    try:
        timer_minutes = int(
            data.get("timer_minutes")
        )
    except (TypeError, ValueError):
        return jsonify({
            "error": "Please select a valid test duration.",
        }), 400

    if timer_minutes < 1 or timer_minutes > 180:
        return jsonify({
            "error": "Invalid test duration selected. Choose a whole number from 1 to 180 minutes.",
        }), 400

    cooldown_remaining = get_generate_cooldown_remaining(
        user_id
    )

    if cooldown_remaining > 0:
        return jsonify({
            "error": (
                f"Please wait {cooldown_remaining} seconds "
                "before generating another mock."
            ),
        }), 429

    mark_generate_attempt(user_id)

    previous_count = None
    if language == "English":
        previous_count = len(
            get_previous_questions(
                user_id,
                topic,
            )
        )

    print(
        f"[MOCK] Generating {''}10-question mock | "
        f"user={user_id} | topic={topic!r} | "
        f"language={language} | timer={timer_minutes}m"
        + (
            f" | previous_questions={previous_count}"
            if language == "English"
            else " | previous_questions=SKIPPED"
        ),
        flush=True,
    )

    try:
        questions_data = generate_unique_mock(
            user_id,
            topic,
            language,
        )

    except ValueError as exc:
        print(
            f"[MOCK] Configuration error: {exc}",
            flush=True,
        )
        return jsonify({
            "error": str(exc),
        }), 500

    except (GroqAPIError, SarvamAPIError) as exc:
        print(
            f"[MOCK] AI error status={exc.status_code}: "
            f"{exc.message}",
            flush=True,
        )

        return jsonify({
            "error": exc.message,
        }), exc.status_code

    # Last validation before persisting.
    if not validate_basic_questions(questions_data):
        return jsonify({
            "error": (
                "AI could not produce a complete "
                "10-question mock. Please try again."
            ),
        }), 502

    # English keeps its existing final historical duplicate safety check.
    # Hindi/Marathi intentionally do not use previous-question storage/checks.
    if language == "English":
        previous_questions = get_previous_questions(
            user_id,
            topic,
        )

        repeated = find_repeated_questions(
            questions_data,
            previous_questions,
        )

        if repeated:
            print(
                "[MOCK] Final duplicate safety check failed.",
                flush=True,
            )

            return jsonify({
                "error": (
                    "A duplicate question was detected. "
                    "Please try again."
                ),
            }), 502

    mock = Mock(
        user_id=user_id,
        topic=topic,
        language=language,
        questions=json.dumps(
            questions_data["questions"],
            ensure_ascii=False,
        ),
        timer_minutes=timer_minutes,
    )

    db.session.add(mock)

    # Count only genuinely created mocks.
    user.mocks_taken_today += 1

    db.session.commit()

    print(
        f"[MOCK] Success | mock_id={mock.id} | "
        f"questions=10 | timer={timer_minutes}m | "
        f"answer_distribution="
        f"{answer_letter_counts(questions_data['questions'])}",
        flush=True,
    )

    return jsonify({
        "mock_id": mock.id,
        "topic": topic,
        "language": language,
        "questions": questions_data["questions"],
        "timer_minutes": timer_minutes,
        "total": 10,
        "cached": False,
    }), 201


# =========================================================
# GET SINGLE MOCK
# =========================================================

@mock_bp.route("/<int:mock_id>", methods=["GET"])
def get_mock(mock_id):
    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "error": "Not authenticated",
        }), 401

    mock = Mock.query.get(mock_id)

    if not mock:
        return jsonify({
            "error": "Mock not found",
        }), 404

    if mock.user_id != user_id:
        user = User.query.get(user_id)

        if not user or not user.is_admin:
            return jsonify({
                "error": "Access denied",
            }), 403

    # A completed mock is a closed exam session. Even if a mobile browser
    # restores an old exam page, the server must never hand the questions
    # back as a fresh attempt.
    if mock.user_id == user_id:
        existing_result = (
            Result.query
            .filter_by(user_id=user_id, mock_id=mock.id)
            .order_by(Result.timestamp.desc())
            .first()
        )

        if existing_result:
            return jsonify({
                "error": "This mock has already been submitted.",
                "result_id": existing_result.id,
            }), 409

    questions = json.loads(mock.questions)

    safe_questions = []

    for question in questions:
        safe_questions.append({
            "question": question["question"],
            "options": question["options"],
        })

    return jsonify({
        "mock_id": mock.id,
        "topic": mock.topic,
        "questions": safe_questions,
        "timer_minutes": mock.timer_minutes or 15,
        "created_at": mock.created_at.isoformat(),
    }), 200


# =========================================================
# HISTORY
# =========================================================

@mock_bp.route("/history", methods=["GET"])
def mock_history():
    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "error": "Not authenticated",
        }), 401

    mocks = (
        Mock.query
        .filter_by(user_id=user_id)
        .order_by(Mock.created_at.desc())
        .limit(20)
        .all()
    )

    return jsonify({
        "mocks": [
            {
                "id": mock.id,
                "topic": mock.topic,
                "created_at": mock.created_at.isoformat(),
            }
            for mock in mocks
        ]
    }), 200
