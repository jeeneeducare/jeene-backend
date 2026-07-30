from typing import Any, Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "error"]


class Chapter(BaseModel):
    node_id: str
    title: str
    chapter_number: int | None
    subject: str | None
    subject_name: str | None
    class_level: int | None


class TreeNode(BaseModel):
    node_id: str
    type: str
    title: str
    description: str | None
    children: list["TreeNode"] = []


class QuestionFigure(BaseModel):
    image_url: str
    placement: str | None
    option_id: str | None
    caption: str | None


class Question(BaseModel):
    question_id: str
    question_type: str
    question_text: str
    options: list[dict[str, Any]] | None
    difficulty: str | None
    figures: list[QuestionFigure] = []
    # The concepts this question is tagged to, so the app can filter a chapter's
    # questions down to one topic without another round trip. Not an answer leak:
    # these are tree node ids, the same ones /chapters/{id}/tree already exposes.
    concept_ids: list[str] = []


class ConceptTag(BaseModel):
    concept_node_id: str
    is_primary: bool


class QuestionAnswer(BaseModel):
    question_id: str
    correct_option_ids: list[str]
    explanation: dict[str, Any] | None
    concepts: list[ConceptTag]


class PaginatedQuestions(BaseModel):
    items: list[Question]
    total: int
    limit: int
    offset: int


class SessionRequest(BaseModel):
    # Sent by the app after a Firebase sign-in. Onboarding captured class/exam;
    # name is asked at first login. All optional so re-login doesn't overwrite.
    display_name: str | None = None
    class_level: int | None = None
    target_exam: str | None = None


class ProfileUpdate(BaseModel):
    display_name: str | None = None
    class_level: int | None = None
    target_exam: str | None = None
    photo_url: str | None = None


class AttemptRequest(BaseModel):
    # Client-generated UUID so a retry cannot record the same answer twice.
    attempt_id: str
    question_id: str
    selected_option_ids: list[str] = []
    numeric_answer: float | None = None
    time_spent_ms: int | None = None
    # Set for a timed test; practice leaves it empty.
    session_id: str | None = None
    # Practice asks for the worked solution back; a test does not, so the answer
    # stays hidden until the test is submitted.
    include_solution: bool = True


class AttemptResult(BaseModel):
    attempt_id: str
    question_id: str
    is_correct: bool
    # Only populated when include_solution was requested.
    correct_option_ids: list[str] | None = None
    explanation: dict[str, Any] | None = None
    # True when this attempt_id was already recorded (a retry), so the client can
    # tell a replay from a fresh grade.
    already_recorded: bool = False


class ConceptProgress(BaseModel):
    node_id: str
    title: str
    attempted: int
    correct: int
    accuracy: float


class ScopeProgress(BaseModel):
    node_id: str
    title: str
    # Attempt counts: how many answers were submitted, and how many were right.
    attempted: int
    correct: int
    accuracy: float
    time_spent_ms: int
    # Coverage: distinct questions touched out of what exists in this scope. This
    # is what a progress ring means ("how far through the material am I"), which
    # is a different question from accuracy.
    total_questions: int = 0
    attempted_questions: int = 0
    solved_questions: int = 0

    @property
    def coverage(self) -> float:
        return round(self.attempted_questions / self.total_questions, 4) if self.total_questions else 0.0


class ChapterProgressDetail(BaseModel):
    chapter: ScopeProgress
    topics: list[ScopeProgress] = []
    # One level below topics: what the subtopic tree screen shows.
    subtopics: list[ScopeProgress] = []
    concepts: list[ConceptProgress] = []


class ProgressSummary(BaseModel):
    attempted: int
    correct: int
    accuracy: float
    time_spent_ms: int
    distinct_questions: int
    subjects: list[ScopeProgress] = []
    chapters: list[ScopeProgress] = []


class UserProfile(BaseModel):
    firebase_uid: str
    tenant_id: str
    display_name: str | None
    email: str | None
    phone: str | None
    class_level: int | None
    target_exam: str | None
    auth_provider: str | None
    photo_url: str | None
    is_new: bool = False
