"""Explicit completion policy, retaining unrelated classifications and statuses."""
from copy import deepcopy

from .config import ConfigError
from .komga import KomgaError
from .metadata import classification_values

KOMGA_STATUSES = {"ONGOING", "ENDED", "HIATUS", "ABANDONED"}


def completion_policy(ongoing_tag="連載中", completed_tag="完結"):
    ongoing = classification_values([ongoing_tag])[0].lower()
    completed = classification_values([completed_tag])[0].lower()
    # Check again after Komga's lowercase conversion (which can expand text).
    classification_values([ongoing, completed])
    if ongoing.casefold() == completed.casefold():
        raise ConfigError("連載中と完結のタグには異なる名前を指定してください。")
    return {"ongoing_tag": ongoing, "completed_tag": completed}


def completed_metadata(before, policy):
    if before["status"] not in KOMGA_STATUSES:
        raise KomgaError("Komgaの連載状態を確認できません。空や不明な状態には反映できません。")
    after = deepcopy(before)
    tags = [tag for tag in before["tags"] if tag.casefold() != policy["ongoing_tag"].casefold()]
    if not any(tag.casefold() == policy["completed_tag"].casefold() for tag in tags):
        tags.append(policy["completed_tag"])
    after.update(tags=sorted(set(tags)), tagsLock=True)
    # Keep curated hiatus/abandoned states and their locks, as KomgaTool does.
    if before["status"] == "ONGOING":
        after.update(status="ENDED", statusLock=True)
    return after
