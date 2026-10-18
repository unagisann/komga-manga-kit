"""Explicit feature registration and shared header navigation."""
from dataclasses import dataclass
from flask import Blueprint


@dataclass(frozen=True)
class NavigationItem:
    label: str
    endpoint: str
    icon: str
    related_endpoints: tuple[str, ...] = ()


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    endpoint: str
    icon: str
    blueprint: Blueprint
    parent: str | None = None
    navigation: tuple[NavigationItem, ...] = ()


def navigation_items(features: list[Feature]) -> list[NavigationItem]:
    items = []
    for feature in features:
        if feature.navigation:
            items.extend(feature.navigation)
        elif not feature.parent:
            items.append(NavigationItem(feature.label, feature.endpoint, feature.icon))
    return items


def built_in_features() -> list[Feature]:
    from .web import home, jobs, library, management, metadata, normalize, organize, roadmap, settings
    from .tools_web import tools
    from .tools_admin import maintenance
    return [Feature("home", "ポータル", "home.index", "home", home),
            Feature("library", "蔵書", "library.index", "search", library),
            Feature("tools", "ポータル機能", "tools.anime", "book", tools, parent="home", navigation=(
                NavigationItem("発見", "tools.discovery", "compass"),
                NavigationItem("新刊", "tools.new_volumes", "square-plus"),
                NavigationItem("今期アニメ", "tools.anime", "tv"),
                NavigationItem("ランキング", "tools.rankings", "chart"),
            )),
            Feature("maintenance", "接続・情報源", "maintenance.settings", "sliders", maintenance, parent="management", navigation=(
                NavigationItem("完結移動", "maintenance.completion", "folder"),
                NavigationItem("表紙修正", "maintenance.page_books", "image", ("maintenance.edit_pages", "maintenance.page_trash")),
            )),
            Feature("normalize", "ノーマライズ", "normalize.index", "refresh", normalize, parent="management"),
            Feature("management", "管理", "management.index", "sliders", management),
            Feature("metadata", "補完・個別確認", "metadata.index", "tag", metadata, parent="management"),
            Feature("organize", "整理ルール", "organize.index", "folder", organize, parent="management"),
            Feature("jobs", "処理履歴", "jobs.index", "refresh", jobs, parent="management"),
            Feature("settings", "設定", "settings.index", "sliders", settings, parent="management"),
            Feature("roadmap", "ロードマップ", "roadmap.index", "route", roadmap, parent="management")]
