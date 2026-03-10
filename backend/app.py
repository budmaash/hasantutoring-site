from __future__ import annotations

import csv
import json
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
import psycopg2
from psycopg2 import sql


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CATEGORY_DB_DIR = DATA_DIR / "category_db"
RESULTS_DIR = BASE_DIR / "results"
TEMPLATE_DIR = BASE_DIR / "WebAppV1_templates"
STATIC_DIR = BASE_DIR / "public"
DB_CONFIG = {
    "host": os.environ.get("SAT_DB_HOST", "localhost"),
    "port": int(os.environ.get("SAT_DB_PORT", "5432")),
    "user": os.environ.get("SAT_DB_USER", "postgres"),
    "password": os.environ.get("SAT_DB_PASSWORD", "3rdtrail"),
    "dbname": os.environ.get("SAT_DB_NAME", "SAT_Database"),
}
DB_ENABLED = os.environ.get("SAT_DB_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
MULTIPLE_CHOICE_CHOICES = ("A", "B", "C", "D")


@dataclass
class Question:
    number: int
    correct_answers: List[str]
    category: str
    expects_numeric_response: bool
    db_question_id: Optional[int] = None

    @property
    def display_correct_answer(self) -> str:
        if not self.correct_answers:
            return ""
        if len(self.correct_answers) == 1:
            return self.correct_answers[0]
        return " or ".join(self.correct_answers)


app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", os.environ.get("SECRET_KEY", "dev-secret-change-me"))


@app.after_request
def add_api_cors_headers(response):
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.context_processor
def inject_globals():
    return {"current_year": datetime.utcnow().year}


@dataclass(frozen=True)
class DatabaseTestMetadata:
    test_id: int
    section_id: int
    module_id: int


@dataclass(frozen=True)
class TestDefinition:
    identifier: str
    name: str
    source: str
    path: Optional[Path] = None
    db_metadata: Optional[DatabaseTestMetadata] = None


_TEST_NUMBER_PATTERN = re.compile(r"(?:test|t)\s*[_\-\s]?(\d+)", re.IGNORECASE)
_MODULE_NUMBER_PATTERN = re.compile(r"(?:module|m)\s*[_\-\s]?(\d+)", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)


def _extract_test_module_numbers(test: TestDefinition) -> Tuple[Optional[str], Optional[str]]:
    test_number: Optional[str] = None
    module_number: Optional[str] = None
    for source in (test.name, test.identifier):
        if not source:
            continue
        if test_number is None:
            match = _TEST_NUMBER_PATTERN.search(source)
            if match:
                test_number = match.group(1)
        if module_number is None:
            match = _MODULE_NUMBER_PATTERN.search(source)
            if match:
                module_number = match.group(1)
    return test_number, module_number


def _build_question_link_prefix(test: TestDefinition) -> Optional[str]:
    test_number, module_number = _extract_test_module_numbers(test)
    if not test_number or not module_number:
        return None
    return f"https://www.hasantutoring.com/math-test-{test_number}-module-{module_number}/v/question"


class QuestionBank:
    """Utility responsible for loading and caching questions for each test."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._questions_cache: Dict[str, List[Question]] = {}
        self._category_lookup = self._load_category_lookup()

    def available_tests(self) -> List[TestDefinition]:
        tests: List[TestDefinition] = []
        tests.extend(self._available_csv_tests())
        tests.extend(self._available_database_tests())
        return tests

    def _available_csv_tests(self) -> List[TestDefinition]:
        tests: List[TestDefinition] = []

        if not self._data_dir.exists():
            return tests

        for csv_path in sorted(self._data_dir.glob("*.csv")):
            identifier = csv_path.stem
            name = csv_path.stem.replace("_", " ").title()
            tests.append(
                TestDefinition(
                    identifier=identifier,
                    name=name,
                    source="csv",
                    path=csv_path,
                )
            )

        return tests

    def _available_database_tests(self) -> List[TestDefinition]:
        tests: List[TestDefinition] = []

        if not DB_ENABLED:
            return tests

        try:
            with psycopg2.connect(**DB_CONFIG) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT DISTINCT
                            q.test_id,
                            t.name,
                            q.section_id,
                            s.name,
                            q.module_id,
                            m.name
                        FROM questions q
                        JOIN tests t ON q.test_id = t.id
                        JOIN sections s ON q.section_id = s.id
                        JOIN modules m ON q.module_id = m.id
                        ORDER BY t.name, s.name, m.name, q.test_id, q.section_id, q.module_id
                        """
                    )
                    for row in cursor.fetchall():
                        test_id, test_name, section_id, section_name, module_id, module_name = row
                        identifier = f"db_{test_id}_{section_id}_{module_id}"
                        display_name = f"{test_name} {section_name} {module_name}"
                        tests.append(
                            TestDefinition(
                                identifier=identifier,
                                name=display_name,
                                source="database",
                                db_metadata=DatabaseTestMetadata(
                                    test_id=test_id,
                                    section_id=section_id,
                                    module_id=module_id,
                                ),
                            )
                        )
        except psycopg2.Error as exc:
            app.logger.warning("Failed to load database tests: %s", exc)

        return tests

    def get_test(self, test_id: str) -> TestDefinition:
        for test in self.available_tests():
            if test.identifier == test_id:
                return test
        raise ValueError(f"Unknown test identifier: {test_id}")

    def questions_for(self, test_id: str) -> List[Question]:
        if test_id in self._questions_cache:
            return self._questions_cache[test_id]

        test = self.get_test(test_id)
        if test.source == "csv":
            if not test.path:
                raise ValueError(f"Test '{test.identifier}' is missing its CSV path.")
            questions = self._load_csv(test.path)
        elif test.source == "database":
            if not test.db_metadata:
                raise ValueError(f"Test '{test.identifier}' is missing database metadata.")
            questions = self._load_database_questions(test.db_metadata)
        else:
            raise ValueError(f"Unknown test source '{test.source}'.")
        self._questions_cache[test_id] = questions
        return questions

    def _load_csv(self, csv_path: Path) -> List[Question]:
        if not csv_path.exists():
            raise FileNotFoundError(
                "Question data file not found. Expected at '{}'".format(csv_path)
            )

        questions: List[Question] = []
        with csv_path.open(newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                try:
                    number = int(row["question_number"].strip())
                except (KeyError, ValueError) as exc:
                    raise ValueError(
                        "Each question must include a numeric 'question_number'."
                    ) from exc

                raw_answer = row.get("correct_answer", "").strip()
                answers, expects_numeric_response = _normalize_answers(raw_answer)
                if not answers:
                    raise ValueError(
                        f"Question {number} is missing a 'correct_answer' entry."
                    )

                category_id_raw = row.get("category_type_id", "").strip()
                if not category_id_raw:
                    raise ValueError(
                        f"Question {number} is missing a 'category_type_id' entry."
                    )

                category_key = _normalize_category_key(category_id_raw)
                category = self._category_lookup.get(category_key)
                if category is None:
                    raise ValueError(
                        "Question {} references an unknown category_type_id '{}'.".format(
                            number, category_id_raw
                        )
                    )

                questions.append(
                    Question(
                        number=number,
                        correct_answers=answers,
                        category=category,
                        expects_numeric_response=expects_numeric_response,
                    )
                )

        questions.sort(key=lambda q: q.number)
        return questions

    def _load_database_questions(self, metadata: DatabaseTestMetadata) -> List[Question]:
        if not DB_ENABLED:
            raise RuntimeError("Database-backed tests are disabled via configuration.")

        query = """
            SELECT
                q.test_question_number,
                q.correct_answer,
                qt.name AS category_name,
                q.id AS question_id
            FROM questions q
            JOIN question_types qt ON q.question_type_id = qt.id
            WHERE q.test_id = %s AND q.section_id = %s AND q.module_id = %s
            ORDER BY q.test_question_number
        """

        questions: List[Question] = []
        try:
            with psycopg2.connect(**DB_CONFIG) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        query,
                        (
                            metadata.test_id,
                            metadata.section_id,
                            metadata.module_id,
                        ),
                    )
                    rows = cursor.fetchall()
        except psycopg2.Error as exc:
            raise RuntimeError(f"Failed to load questions from database: {exc}") from exc

        if not rows:
            raise ValueError("No questions were found for the selected database test.")

        for test_question_number, correct_answer, category_name, question_id in rows:
            if test_question_number is None:
                raise ValueError("Each database question must include a test_question_number.")

            answers, expects_numeric_response = _normalize_answers((correct_answer or "").strip())
            if not answers:
                raise ValueError(
                    f"Question {test_question_number} is missing a 'correct_answer' entry."
                )

            if not category_name:
                raise ValueError(
                    f"Question {test_question_number} is missing a linked question type."
                )

            questions.append(
                Question(
                    number=int(test_question_number),
                    correct_answers=answers,
                    category=category_name,
                    expects_numeric_response=expects_numeric_response,
                    db_question_id=question_id,
                )
            )

        questions.sort(key=lambda q: q.number)
        return questions

    def _load_category_lookup(self) -> Dict[str, str]:
        category_file = CATEGORY_DB_DIR / "SAT_Question_Categories.csv"

        if not category_file.exists():
            raise FileNotFoundError(
                "Category database not found. Expected at '{}'".format(category_file)
            )

        lookup: Dict[str, str] = {}
        with category_file.open(newline="") as csv_file:
            reader = csv.reader(csv_file)
            for row in reader:
                if not row:
                    continue

                raw_key = row[0].strip()
                if not raw_key:
                    continue

                if raw_key.lower() == "index":
                    continue

                if len(row) < 2:
                    raise ValueError(
                        "Category mapping rows must include at least two columns."
                    )

                category_name = row[1].strip()
                if not category_name:
                    raise ValueError(
                        "Category '{}' is missing a name in the mapping file.".format(
                            raw_key
                        )
                    )

                lookup[_normalize_category_key(raw_key)] = category_name

        if not lookup:
            raise ValueError("No categories were loaded from the mapping file.")

        return lookup


def build_score_report(student_answers: Dict[int, str], questions: List[Question]):
    per_question = []
    category_totals: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    correct_count = 0

    for question in questions:
        student_answer = student_answers.get(question.number, "")
        is_correct = student_answer in question.correct_answers

        category_totals[question.category]["total"] += 1
        if is_correct:
            correct_count += 1
            category_totals[question.category]["correct"] += 1

        per_question.append(
            {
                "number": question.number,
                "student_answer": student_answer or "—",
                "raw_student_answer": student_answer,
                "correct_answer": question.display_correct_answer,
                "is_correct": is_correct,
                "category": question.category,
            }
        )

    # SAT math scores range from 200-800. We approximate the scale linearly based on
    # the percentage of questions answered correctly.
    total_questions = len(questions)
    if total_questions:
        accuracy = correct_count / total_questions
        scaled_score = 200 + math.floor(accuracy * 600)
    else:
        accuracy = 0
        scaled_score = 200

    category_breakdown = []
    for category, totals in sorted(category_totals.items()):
        total = totals["total"]
        correct = totals["correct"]
        accuracy_pct = (correct / total * 100) if total else 0
        category_breakdown.append(
            {
                "category": category,
                "correct": correct,
                "total": total,
                "accuracy_pct": accuracy_pct,
            }
        )

    return {
        "per_question": per_question,
        "correct_count": correct_count,
        "total_questions": total_questions,
        "accuracy_pct": accuracy * 100 if total_questions else 0,
        "scaled_score": scaled_score,
        "category_breakdown": category_breakdown,
    }


def _normalize_answers(raw_answer: str) -> Tuple[List[str], bool]:
    tokens = [token.strip() for token in raw_answer.split(";")]
    tokens = [token for token in tokens if token]

    if not tokens:
        return [], False

    expects_numeric = all(_is_numeric_token(token) for token in tokens)

    if expects_numeric:
        normalized = [_normalize_numeric_token(token) for token in tokens]
    else:
        normalized = [token.upper() for token in tokens]

    deduped: List[str] = []
    seen = set()
    for answer in normalized:
        if answer not in seen:
            seen.add(answer)
            deduped.append(answer)

    return deduped, expects_numeric


def _normalize_numeric_token(value: str) -> str:
    return value.strip()


def _is_numeric_token(value: str) -> bool:
    if value is None:
        return False

    trimmed = str(value).strip()
    if not trimmed:
        return False

    signless = trimmed.lstrip("+-").strip()
    if not signless:
        return False

    if " " in signless:
        parts = [part for part in signless.split() if part]
        if len(parts) == 2 and _is_decimal_string(parts[0]) and _is_fraction_string(parts[1]):
            return True
        return False

    if "/" in signless:
        return _is_fraction_string(signless)

    return _is_decimal_string(signless)


def _is_decimal_string(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False

    try:
        Decimal(candidate)
    except (InvalidOperation, ValueError):
        return False

    return True


def _is_fraction_string(value: str) -> bool:
    parts = value.split("/")
    if len(parts) != 2:
        return False

    numerator, denominator = (part.strip() for part in parts)
    if not numerator or not denominator:
        return False

    if not _is_decimal_string(numerator):
        return False

    if not _is_decimal_string(denominator):
        return False

    try:
        return Decimal(denominator) != 0
    except (InvalidOperation, ValueError):
        return False


def _normalize_category_key(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return cleaned

    try:
        return str(int(cleaned))
    except ValueError:
        return cleaned


question_bank = QuestionBank(DATA_DIR)


def _sanitize_filename_segment(value: str) -> str:
    if not value:
        return "student"

    allowed = [ch for ch in value if ch.isalnum() or ch in ("-", "_")]
    sanitized = "".join(allowed).strip("-_")
    return sanitized or "student"


def _save_score_report(report, student_name: str, test: TestDefinition) -> str:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_student = _sanitize_filename_segment(student_name)
    filename = f"{test.identifier}_{safe_student}_{timestamp}.csv"
    destination = RESULTS_DIR / filename

    with destination.open("w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["question_number", "student_answer", "category", "status"])
        for row in report["per_question"]:
            if row["is_correct"]:
                status = "Correct"
            elif not row["raw_student_answer"]:
                status = "Omitted"
            else:
                status = "Incorrect"

            writer.writerow(
                [
                    row["number"],
                    row["raw_student_answer"],
                    row["category"],
                    status,
                ]
            )

    try:
        return str(destination.relative_to(Path.cwd()))
    except ValueError:
        return str(destination)


def _compose_student_name(first_name: str, last_name: str) -> str:
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if first and last:
        return f"{first} {last}"
    return first or last or "Student"


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _is_valid_email(email: str) -> bool:
    return bool(_EMAIL_PATTERN.fullmatch(_normalize_email(email)))


def _persist_submission(
    *,
    test: TestDefinition,
    student_name: str,
    answers: Dict[int, str],
    report,
) -> None:
    if not DB_ENABLED:
        return

    payload_answers = json.dumps(answers)
    payload_report = json.dumps(report)
    payload_categories = json.dumps(report.get("category_breakdown", []))
    created_at = datetime.utcnow()

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO submissions (
                        test_code,
                        student_name,
                        answers_json,
                        results_json,
                        category_json,
                        raw_correct,
                        raw_total,
                        scaled_score,
                        created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        test.identifier,
                        student_name or "Student",
                        payload_answers,
                        payload_report,
                        payload_categories,
                        report.get("correct_count", 0),
                        report.get("total_questions", 0),
                        report.get("scaled_score", 200),
                        created_at,
                    ),
                )
    except psycopg2.Error as exc:
        app.logger.warning("Failed to persist submission to Postgres: %s", exc)


def _next_table_id(cursor, table_name: str) -> int:
    # Serialize manual id assignment to avoid duplicate ids under concurrent writes.
    lock_query = sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE").format(sql.Identifier(table_name))
    cursor.execute(lock_query)
    query = sql.SQL("SELECT COALESCE(MAX(id), 0) + 1 FROM {}").format(sql.Identifier(table_name))
    cursor.execute(query)
    row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"Unable to compute next id for table '{table_name}'.")
    return int(row[0])


def _get_or_create_student_id(cursor, first_name: str, last_name: str, email: str) -> int:
    normalized_email = _normalize_email(email)
    if normalized_email:
        cursor.execute(
            """
            SELECT id, first_name, last_name
            FROM students
            WHERE LOWER(email) = LOWER(%s)
            """,
            (normalized_email,),
        )
        row = cursor.fetchone()
        if row:
            student_id, existing_first_name, existing_last_name = row
            if (
                (existing_first_name or "").strip().lower() != first_name.strip().lower()
                or (existing_last_name or "").strip().lower() != last_name.strip().lower()
            ):
                cursor.execute(
                    """
                    UPDATE students
                    SET first_name = %s, last_name = %s
                    WHERE id = %s
                    """,
                    (first_name, last_name, student_id),
                )
            return student_id

    cursor.execute(
        """
        SELECT id, email
        FROM students
        WHERE LOWER(first_name) = LOWER(%s) AND LOWER(last_name) = LOWER(%s)
        """,
        (first_name, last_name),
    )
    row = cursor.fetchone()
    if row:
        student_id, existing_email = row
        existing_email_normalized = (existing_email or "").strip().lower()
        if normalized_email and not existing_email_normalized:
            cursor.execute(
                """
                UPDATE students
                SET email = %s
                WHERE id = %s
                """,
                (normalized_email, student_id),
            )
            return student_id
        if not normalized_email or existing_email_normalized == normalized_email:
            return student_id
        # Name collision with a different email; create a separate student record.

    new_id = _next_table_id(cursor, "students")
    cursor.execute(
        """
        INSERT INTO students (id, first_name, last_name, email)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (new_id, first_name, last_name, normalized_email),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError("Failed to insert student record.")
    return row[0]


def _persist_student_and_responses(
    *,
    first_name: str,
    last_name: str,
    email: str,
    test: TestDefinition,
    questions: List[Question],
    answers: Dict[int, str],
) -> None:
    if not DB_ENABLED:
        return

    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first or not last:
        return

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                student_id = _get_or_create_student_id(cursor, first, last, email)

                metadata = test.db_metadata
                if metadata:
                    next_response_id = _next_table_id(cursor, "responses")
                    for question in questions:
                        if question.db_question_id is None:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO responses (
                                id,
                                student_id,
                                test_id,
                                section_id,
                                module_id,
                                test_question_number_id,
                                responses
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                next_response_id,
                                student_id,
                                metadata.test_id,
                                metadata.section_id,
                                metadata.module_id,
                                question.db_question_id,
                                answers.get(question.number, ""),
                            ),
                        )
                        next_response_id += 1

            conn.commit()
    except psycopg2.Error as exc:
        app.logger.warning("Failed to persist student/responses: %s", exc)


@app.route("/", methods=["GET", "POST"])
@app.route("/apphome", methods=["GET", "POST"])
def index():
    tests = question_bank.available_tests()
    selected_test_id = tests[0].identifier if tests else None
    first_name = ""
    last_name = ""
    email = ""

    if request.method == "POST":
        if not tests:
            abort(400, description="No test files are available to score.")

        test_id = request.form.get("test_id", "").strip() or selected_test_id
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip()

        if not first_name or not last_name:
            flash("First and last name are required.")
            return render_template(
                "index.html",
                tests=tests,
                selected_test_id=selected_test_id,
                first_name=first_name,
                last_name=last_name,
                email=email,
            )
        if not _is_valid_email(email):
            flash("A valid email address is required.")
            return render_template(
                "index.html",
                tests=tests,
                selected_test_id=selected_test_id,
                first_name=first_name,
                last_name=last_name,
                email=email,
            )

        try:
            question_bank.get_test(test_id)
            selected_test_id = test_id
        except ValueError:
            # Fall back to the default test if an invalid identifier is submitted.
            selected_test_id = tests[0].identifier

        return redirect(
            url_for(
                "entry",
                test_id=selected_test_id,
                first_name=first_name,
                last_name=last_name,
                email=_normalize_email(email),
            )
        )

    if request.method == "GET" and tests:
        requested_test = request.args.get("test_id", "").strip()
        first_name = request.args.get("first_name", "").strip()
        last_name = request.args.get("last_name", "").strip()
        email = request.args.get("email", "").strip()
        if requested_test:
            try:
                question_bank.get_test(requested_test)
                selected_test_id = requested_test
            except ValueError:
                pass

    return render_template(
        "index.html",
        tests=tests,
        selected_test_id=selected_test_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
    )


@app.get("/api/tests")
def api_tests():
    tests = question_bank.available_tests()
    return jsonify(
        [
            {
                "identifier": test.identifier,
                "name": test.name,
                "source": test.source,
            }
            for test in tests
        ]
    )


@app.get("/entry")
def entry():
    test_id = request.args.get("test_id", "").strip()
    first_name = request.args.get("first_name", "").strip()
    last_name = request.args.get("last_name", "").strip()
    email = request.args.get("email", "").strip()
    student_name = _compose_student_name(first_name, last_name)

    if not test_id:
        abort(400, description="A test must be selected before entering answers.")
    if not first_name or not last_name:
        abort(400, description="First and last name are required to score a student.")
    if not _is_valid_email(email):
        abort(400, description="A valid email address is required to score a student.")

    try:
        test = question_bank.get_test(test_id)
    except ValueError as exc:
        abort(400, description=str(exc))

    questions = question_bank.questions_for(test_id)

    return render_template(
        "entry.html",
        test=test,
        student_name=student_name,
        first_name=first_name,
        last_name=last_name,
        email=_normalize_email(email),
        questions=questions,
        multiple_choice_choices=MULTIPLE_CHOICE_CHOICES,
    )


@app.post("/results")
def results():
    test_id = request.form.get("test_id", "").strip()
    if not test_id:
        abort(400, description="A test must be selected to score responses.")

    try:
        test = question_bank.get_test(test_id)
    except ValueError as exc:
        abort(400, description=str(exc))

    questions = question_bank.questions_for(test_id)
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    email = request.form.get("email", "").strip()
    if not first_name or not last_name:
        abort(400, description="First and last name are required to score a student.")
    if not _is_valid_email(email):
        abort(400, description="A valid email address is required to score a student.")
    student_name = _compose_student_name(first_name, last_name)

    answers: Dict[int, str] = {}
    for question in questions:
        answer = request.form.get(f"q_{question.number}", "").strip()
        if not question.expects_numeric_response:
            answer = answer.upper()
        answers[question.number] = answer

    report = build_score_report(answers, questions)

    _persist_student_and_responses(
        first_name=first_name,
        last_name=last_name,
        email=_normalize_email(email),
        test=test,
        questions=questions,
        answers=answers,
    )

    saved_report_path = _save_score_report(report, student_name, test)
    _persist_submission(test=test, student_name=student_name, answers=answers, report=report)

    return render_template(
        "results.html",
        student_name=student_name,
        test_id=test.identifier,
        test_name=test.name,
        report=report,
        report_csv_path=saved_report_path,
        first_name=first_name,
        last_name=last_name,
        email=_normalize_email(email),
        question_link_prefix=_build_question_link_prefix(test),
    )


@app.get("/ss_homepage")
def ss_homepage_shell():
    return render_template("ss_homepage_shell.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
