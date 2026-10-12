from dataclasses import asdict, dataclass, field
import hashlib

from .japanese import kana_row


@dataclass
class Book:
    path: str
    volume: str | None = None
    size: int = 0
    source: str = "filename"


@dataclass
class Series:
    path: str
    title: str
    author: str = ""
    reading: str = ""
    status: str = "unknown"
    summary: str = ""
    publisher: str = ""
    books: list[Book] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return hashlib.sha256(self.path.encode()).hexdigest()[:16]

    @property
    def row(self) -> str:
        return kana_row(self.reading or self.title)

    def to_dict(self) -> dict:
        return {**asdict(self), "id": self.id, "kana_row": self.row, "book_count": len(self.books),
                "metadata_complete": bool(self.author and self.reading and self.summary)}


@dataclass
class ScanResult:
    series: list[Series]
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    def stats(self) -> dict:
        return {"series": len(self.series), "books": sum(len(s.books) for s in self.series),
                "completed": sum(s.status == "completed" for s in self.series),
                "needs_metadata": sum(not (s.author and s.reading and s.summary) for s in self.series),
                "warnings": len(self.warnings) + sum(len(s.warnings) for s in self.series)}
