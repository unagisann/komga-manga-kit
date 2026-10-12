"""Explicit feature registration: a blueprint and navigation entry per module."""
from dataclasses import dataclass
from flask import Blueprint


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    endpoint: str
    icon: str
    blueprint: Blueprint


def built_in_features() -> list[Feature]:
    from .web import home, library, metadata, organize, roadmap, settings
    return [Feature("home", "ホーム", "home.index", "home", home),
            Feature("library", "蔵書", "library.index", "book", library),
            Feature("organize", "蔵書整理", "organize.index", "folder", organize),
            Feature("metadata", "メタデータ", "metadata.index", "tag", metadata),
            Feature("settings", "設定", "settings.index", "sliders", settings),
            Feature("roadmap", "ロードマップ", "roadmap.index", "route", roadmap)]
