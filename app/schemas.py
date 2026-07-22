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


class UserProfile(BaseModel):
    firebase_uid: str
    tenant_id: str
    display_name: str | None
    email: str | None
    phone: str | None
    class_level: int | None
    target_exam: str | None
    auth_provider: str | None
    is_new: bool = False
