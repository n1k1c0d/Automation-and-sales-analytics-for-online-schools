import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

EMAIL = re.compile(r"[\w.+%'-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(
    r"(?<!\w)(?:\+(?:[ .()\-–—]*\d){9,15}|"
    r"(?:[78][ .\-–—]*)?(?:\(\d{3}\)|\d{3})[ .\-–—]*"
    r"\d{3}[ .\-–—]*\d{2}[ .\-–—]*\d{2})(?!\w)"
)
NAME_LABEL = re.compile(
    r"\b(?:фио|фамилия|имя|name)\s*[:=—–-]\s*[^\W\d_]+", re.IGNORECASE
)
GIVEN_NAME = re.compile(
    r"\b(?:Александр[а]?|Алексей|Анастасия|Андрей|Анна|Василий|Виктор(?:ия)?|"
    r"Дмитрий|Екатерина|Елена|Иван|Ирина|Константин|Мария|Михаил|Наталья|"
    r"Николай|Ольга|Павел|П[её]тр|Сергей|Татьяна|Юлия|"
    r"Alice|Anna|Jane|John|Maria|Michael|Robert)\b"
)
NAME_WITH_SURNAME = re.compile(
    r"\b(?:[А-ЯЁ][а-яё]{1,}|[А-ЯЁ]\.[ \t]*[А-ЯЁ]\.)[ \t]*"
    r"[А-ЯЁ][а-яё]+(?:ов[а]?|[её]в[а]?|ин[а]?|ск(?:ий|ая)|енко|ич)\b"
)


@dataclass(frozen=True, slots=True)
class CommentFinding:
    comment_id: int
    categories: tuple[str, ...]


class CommentPrivacyError(ValueError):
    def __init__(self, findings: tuple[CommentFinding, ...]) -> None:
        self.findings = findings
        locations = ", ".join(
            f"{finding.comment_id} ({', '.join(finding.categories)})"
            for finding in findings
        )
        super().__init__(
            f"Potential personal data in comments {locations}. Review and anonymize the input before sending."
        )


def check_comment_privacy(comments: Sequence[str]) -> None:
    findings = []
    for comment_id, comment in enumerate(comments, start=1):
        normalized = unicodedata.normalize("NFKC", comment)
        text = "".join(
            char for char in normalized if unicodedata.category(char) != "Cf"
        )
        categories = []
        if EMAIL.search(text):
            categories.append("email")
        if PHONE.search(text):
            categories.append("phone")
        if any(
            pattern.search(text)
            for pattern in (NAME_LABEL, GIVEN_NAME, NAME_WITH_SURNAME)
        ):
            categories.append("name")
        if categories:
            findings.append(CommentFinding(comment_id, tuple(categories)))
    if findings:
        raise CommentPrivacyError(tuple(findings))
