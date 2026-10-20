"""Explicit feature registration, shared header navigation, portal links and management tabs."""
from dataclasses import dataclass
from flask import Blueprint


@dataclass(frozen=True)
class NavigationItem:
    label: str
    endpoint: str
    icon: str
    related_endpoints: tuple[str, ...] = ()
    # Shown in the header until the user arranges the menu themselves.
    shown: bool = True

    @property
    def id(self):
        return self.endpoint


@dataclass(frozen=True)
class PortalLink:
    id: str
    label: str
    endpoint: str
    icon: str
    description: str
    # Shown on the portal until the user arranges the menu themselves.
    shown: bool = True


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    endpoint: str
    icon: str
    blueprint: Blueprint
    parent: str | None = None
    navigation: tuple[NavigationItem, ...] = ()
    portal: tuple[PortalLink, ...] = ()


# The 管理 page is split like the existing Tools: run maintenance, change settings, automation and history.
# Related entries ending in "." match a whole blueprint; exact endpoints win over those prefixes.
MANAGEMENT_TABS = (
    NavigationItem("メンテナンス", "management.index", "sliders", ("management.", "normalize.", "metadata.", "maintenance.")),
    NavigationItem("環境設定", "settings.index", "plug", ("settings.", "organize.", "maintenance.settings")),
    NavigationItem("自動更新", "maintenance.automation", "clock"),
    NavigationItem("処理履歴", "jobs.index", "refresh", ("jobs.",)),
)


def management_tab(endpoint):
    endpoint = endpoint or ""
    exact = next((tab for tab in MANAGEMENT_TABS if endpoint == tab.endpoint or endpoint in tab.related_endpoints), None)
    return exact or next((tab for tab in MANAGEMENT_TABS
                          if any(name.endswith(".") and endpoint.startswith(name) for name in tab.related_endpoints)), None)


def navigation_items(features: list[Feature]) -> list[NavigationItem]:
    items = []
    for feature in features:
        if feature.navigation:
            items.extend(feature.navigation)
        elif not feature.parent:
            items.append(NavigationItem(feature.label, feature.endpoint, feature.icon))
    return items


def portal_links(features: list[Feature]) -> list[PortalLink]:
    return [link for feature in features for link in feature.portal]


def built_in_features() -> list[Feature]:
    from .web import home, jobs, library, management, metadata, normalize, organize, roadmap, settings
    from .tools_web import tools
    from .tools_admin import maintenance
    return [Feature("home", "ポータル", "home.index", "home", home, portal=(
                # Komga opens the library itself, or leads to the connection settings when it is not set up.
                PortalLink("komga", "Komgaで読む", "", "book", "ライブラリ本体をブラウザで開く"),)),
            Feature("library", "蔵書", "library.index", "search", library, portal=(
                PortalLink("library", "蔵書を探す", "library.index", "search", "読者層・出版社・年代・評価で絞り込み"),)),
            Feature("tools", "ポータル機能", "tools.anime", "book", tools, parent="home", navigation=(
                NavigationItem("発見", "tools.discovery", "compass"),
                NavigationItem("新刊", "tools.new_volumes", "square-plus"),
                NavigationItem("今期アニメ", "tools.anime", "tv"),
                NavigationItem("ランキング", "tools.rankings", "chart", ("tools.ranking_year",)),
            ), portal=(
                PortalLink("discovery", "発見", "tools.discovery", "compass", "紹介記事・楽天Kobo売上・マンバ通信・コミックナタリー"),
                PortalLink("new_volumes", "新刊チェック", "tools.new_volumes", "square-plus", "所持シリーズの新刊・予約状況（楽天Books＋Kobo）"),
                PortalLink("anime", "今期のアニメ", "tools.anime", "tv", "放送中アニメの原作を所持判定。持っている作品はすぐ読める"),
                PortalLink("rankings", "次にくるマンガ大賞", "tools.rankings", "chart", "歴代ランキング（2015〜）を所持状況つきで"),
            )),
            Feature("maintenance", "接続・情報源", "maintenance.settings", "sliders", maintenance, parent="management", navigation=(
                NavigationItem("書籍移動", "maintenance.moves", "folder", ("maintenance.completion",)),
                NavigationItem("表紙修正", "maintenance.page_books", "image", ("maintenance.edit_pages", "maintenance.page_trash")),
                NavigationItem("自動更新", "maintenance.automation", "clock", shown=False),
            ), portal=(
                PortalLink("pages", "表紙・ページ修正", "maintenance.page_books", "image", "表紙変更・並べ替え・不要ページの削除と復元", shown=False),
                PortalLink("moves", "書籍移動", "maintenance.moves", "folder", "整理ルールの場所と違う作品を、衝突を確認して移す", shown=False),
                PortalLink("automation", "自動更新", "maintenance.automation", "clock", "情報源の更新と取り込み後の処理を毎日実行", shown=False),
            )),
            Feature("normalize", "取り込み整理", "normalize.index", "refresh", normalize, parent="management", navigation=(
                NavigationItem("取り込み整理", "normalize.index", "refresh", shown=False),
            ), portal=(
                PortalLink("normalize", "追加した漫画を整理", "normalize.index", "refresh", "CBZ化・ページ順・名前と配置をまとめて整える"),)),
            Feature("management", "管理", "management.index", "sliders", management, portal=(
                PortalLink("management", "管理", "management.index", "sliders", "補完・表紙修正・書籍移動・処理履歴・設定"),)),
            Feature("metadata", "補完・個別確認", "metadata.index", "tag", metadata, parent="management", portal=(
                PortalLink("metadata", "作品情報の補完", "metadata.index", "tag", "読み・著者・出版社などの不足情報を取得して確認", shown=False),)),
            Feature("organize", "整理ルール", "organize.index", "folder", organize, parent="management"),
            Feature("jobs", "処理履歴", "jobs.index", "refresh", jobs, parent="management", navigation=(
                NavigationItem("処理履歴", "jobs.index", "refresh", ("jobs.detail",), shown=False),
            ), portal=(
                PortalLink("jobs", "処理履歴", "jobs.index", "refresh", "処理の進み具合・結果と、復元への入口", shown=False),)),
            Feature("settings", "設定", "settings.index", "sliders", settings, parent="management", navigation=(
                NavigationItem("環境設定", "settings.index", "plug", ("maintenance.settings",), shown=False),
            ), portal=(
                PortalLink("settings", "環境設定", "settings.index", "plug", "接続・情報源・AI・表示するメニュー", shown=False),)),
            Feature("roadmap", "ロードマップ", "roadmap.index", "route", roadmap, parent="management")]
