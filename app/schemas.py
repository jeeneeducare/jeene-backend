from datetime import date, datetime
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


class TestSummary(BaseModel):
    test_id: str
    title: str
    exam: str | None = None
    class_levels: list[int] = []
    duration_minutes: int | None = None
    marks_correct: int = 4
    marks_wrong: int = 1
    question_count: int = 0


class TestPaper(BaseModel):
    """A question as it appears in a paper. Deliberately carries no answer."""
    position: int
    section: str | None = None
    question_id: str
    question_type: str
    question_text: str
    options: list[dict[str, Any]] | None = None
    difficulty: str | None = None
    # Some questions are unanswerable without their diagram, so the paper carries them.
    figures: list[QuestionFigure] = []


class TestSession(BaseModel):
    session_id: str
    # Only ever populated when a handoff code is claimed: the browser's credential for
    # this one sitting. Never returned to the app, which already holds a real token.
    web_token: str | None = None
    test_id: str
    title: str
    # The student sitting the paper. The browser has no account of its own, so the name
    # on the CBT band has to come with the sitting.
    candidate_name: str | None = None
    handoff_code: str | None = None
    started_at: datetime
    expires_at: datetime
    submitted_at: datetime | None = None
    duration_minutes: int | None = None
    marks_correct: int = 4
    marks_wrong: int = 1
    marked_for_review: list[str] = []
    # question_id -> the options chosen, so a resumed sitting restores its answers.
    responses: dict[str, list[str]] = {}
    paper: list[TestPaper] = []


class QuestionResult(BaseModel):
    """One row of the post-submission review.

    Only ever built for a submitted sitting, which is what makes it safe to carry the
    key and the worked solution: the paper is over.
    """
    position: int
    section: str | None = None
    question_id: str
    question_text: str
    options: list[dict[str, Any]] | None = None
    figures: list[QuestionFigure] = []
    selected_option_ids: list[str] = []
    correct_option_ids: list[str] = []
    explanation: str | None = None
    # 'correct' | 'wrong' | 'skipped'
    status: str


class TestSessionResult(BaseModel):
    session_id: str
    test_id: str
    submitted_at: datetime | None = None
    total_questions: int
    correct_count: int
    wrong_count: int
    skipped_count: int
    score: float
    max_score: float
    title: str | None = None
    marks_correct: int = 4
    marks_wrong: int = 1
    review: list[QuestionResult] = []

# --- Reports ---------------------------------------------------------------------
#
# One payload per screen rather than per card: the analytics screen is a single scroll,
# so it should cost a single round trip, and every number on it comes from the same
# attempt log at the same instant. Split across calls they could disagree.


class DayStudy(BaseModel):
    """One bar of the study-time chart. `day` is an IST date, not UTC."""
    day: date
    label: str
    minutes: int
    questions: int
    correct: int = 0
    accuracy: float = 0.0


class HourAccuracy(BaseModel):
    """Accuracy in one hour of the IST clock, for "when do you work best"."""
    hour: int
    attempted: int
    correct: int
    accuracy: float


class SubjectSlice(BaseModel):
    subject_id: str
    title: str
    attempted: int
    correct: int
    accuracy: float
    minutes: int
    total_questions: int
    attempted_questions: int
    solved_questions: int
    coverage: float


class DifficultySlice(BaseModel):
    """How the bank splits by difficulty, and how much of each the student has solved."""
    difficulty: str
    total: int
    solved: int
    attempted: int


class TimeSplit(BaseModel):
    """Where the time went. Only two things a student can do, so only two slices."""
    practice_minutes: int
    test_minutes: int


class StreakInfo(BaseModel):
    """Days in a row meeting the daily bar, which is minutes studied, not questions."""
    current: int
    longest: int
    minutes_today: int
    goal_minutes: int
    met_today: bool


class ReportSummary(BaseModel):
    period: str
    # Absent until there is a previous period to compare with; the client hides the
    # delta rather than showing a number it cannot justify.
    since: datetime | None = None
    attempted: int
    correct: int
    wrong: int
    accuracy: float
    distinct_questions: int
    minutes: int
    pyq_attempted: int
    pyq_correct: int
    total_questions: int
    solved_questions: int
    coverage: float
    subjects: list[SubjectSlice] = []
    difficulty: list[DifficultySlice] = []
    time_split: TimeSplit
    by_day: list[DayStudy] = []
    by_hour: list[HourAccuracy] = []
    streak: StreakInfo
    # Same figures for the period before this one, when one exists. Null means the
    # student has no history yet, and the screen shows no comparison at all.
    previous_attempted: int | None = None
    previous_minutes: int | None = None


class QuestionExplanation(BaseModel):
    """The plain-language explanation shown under "Understand with AI".

    Written ahead of time by the pipeline from the teacher's own worked solution, and
    read back here as an ordinary row. Nothing is generated while a student waits.
    """
    question_id: str
    text: str


class WeakTopic(BaseModel):
    """One topic a student keeps getting wrong, and where to go and fix it.

    Carries `chapter_id` and `concept_ids` because the point of the screen is the jump:
    tapping a row has to open the practice deck already narrowed to this topic, and the
    app filters a chapter's questions by concept locally rather than asking again.
    """
    topic_id: str
    title: str
    chapter_id: str
    chapter_title: str
    subject_id: str
    subject_title: str
    concept_ids: list[str] = []
    attempted: int
    wrong: int
    accuracy: float
    # Of the questions ever answered wrong here, how many are still wrong on the most
    # recent try. This is the number the screen leads with: it is the work remaining.
    unfixed: int
    practice_wrong: int
    test_wrong: int
    last_wrong_at: datetime | None = None


class SubjectWeakness(BaseModel):
    subject_id: str
    title: str
    attempted: int
    wrong: int
    accuracy: float


class MistakeBook(BaseModel):
    """Everything the Mistake Book screen shows, from one read of the attempt log."""
    attempted: int
    ever_wrong: int
    unfixed: int
    fixed: int
    accuracy: float
    practice_wrong: int
    test_wrong: int
    topics_affected: int
    subjects: list[SubjectWeakness] = []
    topics: list[WeakTopic] = []


class QuestionHistory(BaseModel):
    """How a student left one question the last time they met it.

    Carries the key and the worked solution, which is only safe because a row exists
    here at all: an attempt is recorded when a question is graded, so by the time this
    can be returned the student has already been shown the answer. Nothing is revealed
    that they have not already earned.
    """
    question_id: str
    selected_option_ids: list[str] = []
    is_correct: bool
    correct_option_ids: list[str] = []
    explanation: str = ""
    attempted_at: datetime
    # True when the attempt came from a mock paper rather than from practice. The
    # navigator does not distinguish them, but the deck can say where a mark came from.
    in_a_test: bool = False


class ChapterNotes(BaseModel):
    """The teacher's written notes for a chapter, as a hosted PDF.

    One document per chapter rather than per topic. Topic notes do not exist yet, and a
    student opening Notes from a topic is better served the chapter's than nothing.
    """
    chapter_id: str
    title: str
    pdf_url: str
    page_count: int
    size_bytes: int


class ChapterVideo(BaseModel):
    """One curated YouTube lecture for a chapter.

    Ids are curated rather than searched for at runtime, so what a student sees is what
    somebody chose. The title and channel come from YouTube itself at the time it was
    added, so the tile reads the same as the video it opens.
    """
    youtube_id: str
    title: str
    channel: str = ""
    thumbnail_url: str = ""
    # The page the apps load to play this. Built by the server rather than the app so the
    # player can be fixed or moved without an app release, which the three attempts it
    # took to get playback working made a very concrete argument for.
    player_url: str = ""


class AdminVideo(BaseModel):
    """A video as the admin panel sees it: with its status and who attached it."""
    node_id: str
    youtube_id: str
    title: str
    channel: str = ""
    thumbnail_url: str = ""
    position: int = 0
    status: str = "draft"
    added_by: str = ""
    added_at: datetime


class AdminNode(BaseModel):
    """One place in the tree, with whatever hangs off it."""
    node_id: str
    type: str
    title: str
    videos: list[AdminVideo] = []
    children: list["AdminNode"] = []


class ChapterSummary(BaseModel):
    node_id: str
    title: str
    subject_id: str | None = None
    subject_title: str = ""
    class_level: int | None = None
    status: str = "draft"
    video_count: int = 0
