import fnmatch
from uuid import uuid4

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal, Slot
from PySide6.QtGui import QColor

from utils.settings import (get_tag_library_aliases, get_tag_library_categories,
                            get_tag_library_category_by_tag,
                            get_tag_library_implications,
                            get_tag_library_profiles, get_tag_library_tags,
                            save_tag_library_aliases, save_tag_library_categories,
                            save_tag_library_category_by_tag,
                            save_tag_library_implications,
                            save_tag_library_profiles,
                            save_tag_library_tags)


class TagLibraryModel(QAbstractListModel):
    aliases_changed = Signal()
    categories_changed = Signal()
    implications_changed = Signal()
    new_tags_added = Signal(list)
    profiles_changed = Signal()

    def __init__(self):
        super().__init__()
        self.tags = get_tag_library_tags()
        self._tags_set: set[str] = set(self.tags)
        self.categories = get_tag_library_categories()
        self.category_by_tag = get_tag_library_category_by_tag()
        self.aliases: dict[str, str] = get_tag_library_aliases()
        self.implications: dict[str, list[str]] = get_tag_library_implications()
        self.profiles: dict[str, dict[str, str]] = get_tag_library_profiles()
        self._prune_orphaned_category_assignments()

    def rowCount(self, parent=None) -> int:
        return len(self.tags)

    def data(self, index: QModelIndex, role=None) -> str | tuple[str, str] | QColor | None:
        if not index.isValid():
            return None
        tag = self.tags[index.row()]
        category = self.get_category_for_tag(tag)
        category_name = category['name'] if category else ''
        if role == Qt.ItemDataRole.DisplayRole:
            if category_name:
                return f'{tag} ({category_name})'
            return tag
        if role == Qt.ItemDataRole.EditRole:
            return tag
        if role == Qt.ItemDataRole.UserRole:
            return tag, category_name
        if role == Qt.ItemDataRole.ToolTipRole:
            return f'{tag} ({category_name})' if category_name else tag
        if role == Qt.ItemDataRole.ForegroundRole and category:
            color = QColor(category['color'])
            if color.isValid():
                return color
        return None

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        normalized_tags = []
        seen_tags = set()
        for tag in tags:
            normalized_tag = tag.strip()
            if not normalized_tag or normalized_tag in seen_tags:
                continue
            normalized_tags.append(normalized_tag)
            seen_tags.add(normalized_tag)
        return normalized_tags

    def _normalize_color(self, color: str) -> str:
        normalized_color = QColor(color)
        if not normalized_color.isValid():
            return ''
        return normalized_color.name()

    def _prune_orphaned_category_assignments(self):
        category_ids = {category['id'] for category in self.categories}
        self.category_by_tag = {
            tag: category_id for tag, category_id in self.category_by_tag.items()
            if tag in self._tags_set and category_id in category_ids
        }
        self._save_all_tag_library_data()

    def _cleanup_relationships_for_removed_tags(
            self, removed: set[str]) -> tuple[bool, bool]:
        """Drop aliases/implications that reference removed tags.

        Returns (aliases_changed, implications_changed) so the caller can emit
        the matching signals after the data has been saved.
        """
        aliases_changed = False
        kept_aliases = {}
        for alias, canonical in self.aliases.items():
            # An alias whose canonical target no longer exists is broken.
            if canonical in removed:
                aliases_changed = True
                continue
            kept_aliases[alias] = canonical
        if aliases_changed:
            self.aliases = kept_aliases

        implications_changed = False
        kept_implications = {}
        for trigger, implied in self.implications.items():
            # A rule triggered by a removed tag can never fire again.
            if trigger in removed:
                implications_changed = True
                continue
            kept = [tag for tag in implied if tag not in removed]
            if len(kept) != len(implied):
                implications_changed = True
            # Drop rules left with no implied tags.
            if kept:
                kept_implications[trigger] = kept
            elif implied:
                implications_changed = True
        if implications_changed:
            self.implications = kept_implications

        return aliases_changed, implications_changed

    def _rename_tag_in_relationships(
            self, old_tags: set[str], new_tag: str) -> tuple[bool, bool]:
        """Repoint aliases/implications from old tag names to new_tag.

        Returns (aliases_changed, implications_changed) so the caller can emit
        the matching signals after the data has been saved.
        """
        aliases_changed = False
        renamed_aliases = {}
        for alias, canonical in self.aliases.items():
            new_canonical = new_tag if canonical in old_tags else canonical
            if new_canonical != canonical:
                aliases_changed = True
            # A rename can make an alias point to itself; drop it if so.
            if alias == new_canonical:
                aliases_changed = True
                continue
            renamed_aliases[alias] = new_canonical
        if aliases_changed:
            self.aliases = renamed_aliases

        implications_changed = False
        renamed_implications: dict[str, list[str]] = {}
        for trigger, implied in self.implications.items():
            new_trigger = new_tag if trigger in old_tags else trigger
            if new_trigger != trigger:
                implications_changed = True
            remapped: list[str] = []
            for tag in implied:
                new_implied = new_tag if tag in old_tags else tag
                if new_implied != tag:
                    implications_changed = True
                # Skip self-references and duplicates created by the rename.
                if new_implied != new_trigger and new_implied not in remapped:
                    remapped.append(new_implied)
                elif new_implied == new_trigger:
                    implications_changed = True
            if not remapped:
                if implied:
                    implications_changed = True
                continue
            if new_trigger in renamed_implications:
                # Merge into an existing rule for the new trigger.
                implications_changed = True
                for tag in remapped:
                    if tag not in renamed_implications[new_trigger]:
                        renamed_implications[new_trigger].append(tag)
            else:
                renamed_implications[new_trigger] = remapped
        if implications_changed:
            self.implications = renamed_implications

        return aliases_changed, implications_changed


    def _save_all_tag_library_data(self):
        save_tag_library_tags(self.tags)
        save_tag_library_categories(self.categories)
        save_tag_library_category_by_tag(self.category_by_tag)
        save_tag_library_aliases(self.aliases)
        save_tag_library_implications(self.implications)
        save_tag_library_profiles(self.profiles)

    def _notify_model_changed(self):
        self.beginResetModel()
        self.endResetModel()

    def get_categories(self) -> list[dict[str, str]]:
        return [category.copy() for category in self.categories]

    def get_aliases(self) -> dict[str, str]:
        """Returns a copy of the alias dict (alias → canonical)."""
        return dict(self.aliases)

    def get_implications(self) -> dict[str, list[str]]:
        """Returns a copy of the implications dict."""
        return {tag: list(implied) for tag, implied in self.implications.items()}

    def get_profiles(self) -> dict[str, dict[str, str]]:
        """Returns a copy of the profiles dict."""
        return {name: dict(mapping) for name, mapping in self.profiles.items()}

    def get_category_order_map(self) -> dict[str, int]:
        return {
            category['id']: index
            for index, category in enumerate(self.categories)
        }

    def has_tag(self, tag: str) -> bool:
        return tag.strip() in self._tags_set

    def get_category_for_tag(self, tag: str) -> dict[str, str] | None:
        category_id = self.category_by_tag.get(tag)
        if not category_id:
            return None
        for category in self.categories:
            if category['id'] == category_id:
                return category
        return None

    def add_alias(self, alias: str, canonical: str) -> bool:
        """Add alias → canonical. Returns False if alias already exists."""
        alias = alias.strip()
        canonical = canonical.strip()
        if not alias or not canonical or alias == canonical:
            return False
        if alias in self.aliases:
            return False
        self.aliases[alias] = canonical
        self._save_all_tag_library_data()
        self.aliases_changed.emit()
        return True

    def remove_aliases(self, aliases: list[str]):
        changed = False
        for alias in aliases:
            if alias in self.aliases:
                del self.aliases[alias]
                changed = True
        if changed:
            self._save_all_tag_library_data()
            self.aliases_changed.emit()

    def add_implication(self, tag: str, implied_tags: list[str]) -> bool:
        """Add or replace the implied tags for a given tag. Returns False if nothing to add."""
        tag = tag.strip()
        implied_tags = [t.strip() for t in implied_tags if t.strip() and t.strip() != tag]
        if not tag or not implied_tags:
            return False
        existing = self.implications.get(tag, [])
        new_tags = [t for t in implied_tags if t not in existing]
        if not new_tags:
            return False
        self.implications[tag] = existing + new_tags
        self._save_all_tag_library_data()
        self.implications_changed.emit()
        return True

    def remove_implication_rule(self, tag: str):
        """Remove all implications for a given trigger tag."""
        if tag in self.implications:
            del self.implications[tag]
            self._save_all_tag_library_data()
            self.implications_changed.emit()

    def add_profile(self, name: str) -> bool:
        """Create a new empty profile. Returns False if name already exists."""
        name = name.strip()
        if not name or name in self.profiles:
            return False
        self.profiles[name] = {}
        self._save_all_tag_library_data()
        self.profiles_changed.emit()
        return True

    def rename_profile(self, old_name: str, new_name: str) -> bool:
        """Rename a profile. Returns False if old doesn't exist or new already exists."""
        new_name = new_name.strip()
        if not new_name or old_name not in self.profiles or new_name == old_name:
            return False
        if new_name in self.profiles:
            return False
        self.profiles = {
            (new_name if key == old_name else key): value
            for key, value in self.profiles.items()
        }
        self._save_all_tag_library_data()
        self.profiles_changed.emit()
        return True

    def remove_profile(self, name: str):
        """Remove a profile entirely."""
        if name in self.profiles:
            del self.profiles[name]
            self._save_all_tag_library_data()
            self.profiles_changed.emit()

    def set_profile_mapping(self, profile_name: str, original: str,
                            replacement: str) -> bool:
        """Add or update a mapping in a profile. Returns False if profile doesn't exist."""
        original = original.strip()
        replacement = replacement.strip()
        if not profile_name or profile_name not in self.profiles:
            return False
        if not original:
            return False
        self.profiles[profile_name][original] = replacement
        self._save_all_tag_library_data()
        self.profiles_changed.emit()
        return True

    def remove_profile_mappings(self, profile_name: str, originals: list[str]):
        """Remove specific mappings from a profile."""
        if profile_name not in self.profiles:
            return
        changed = False
        for original in originals:
            if original in self.profiles[profile_name]:
                del self.profiles[profile_name][original]
                changed = True
        if changed:
            self._save_all_tag_library_data()
            self.profiles_changed.emit()

    def get_implied_tags(self, tags: list[str]) -> list[str]:
        """Return all tags implied by the given list, excluding tags already in the list.

        Trigger rules can be exact tags or glob patterns containing ``*`` or ``?``.
        Exact triggers match case-sensitively; wildcard triggers match
        case-insensitively via fnmatch.
        """
        tags_set = set(tags)
        implied = []
        seen = set()
        # Precompute wildcard rules once so we don't rescan the whole dict per tag.
        wildcard_rules = [(trigger, implied_tags)
                          for trigger, implied_tags in self.implications.items()
                          if '*' in trigger or '?' in trigger]

        def add_implied(implied_tags: list[str]):
            for implied_tag in implied_tags:
                if implied_tag not in tags_set and implied_tag not in seen:
                    implied.append(implied_tag)
                    seen.add(implied_tag)

        for tag in tags:
            add_implied(self.implications.get(tag, []))
            tag_lower = tag.lower()
            for trigger, implied_tags in wildcard_rules:
                if fnmatch.fnmatchcase(tag_lower, trigger.lower()):
                    add_implied(implied_tags)
        return implied

    @Slot(list)
    def add_tags(self, tags: list[str]):
        if not tags:
            return
        normalized_new_tags = self._normalize_tags(tags)
        if not normalized_new_tags:
            return
        new_unique_tags = [tag for tag in normalized_new_tags
                           if tag not in self._tags_set]
        if not new_unique_tags:
            return
        merged_tags = list(reversed(new_unique_tags)) + self.tags
        if merged_tags == self.tags:
            return
        self.beginResetModel()
        self.tags = merged_tags
        self._tags_set = set(self.tags)
        self.endResetModel()
        self._prune_orphaned_category_assignments()
        self.new_tags_added.emit(new_unique_tags)

    @Slot(list)
    def remove_tags(self, tags: list[str]):
        if not tags:
            return
        tags_to_remove = {tag.strip() for tag in tags if tag.strip()}
        if not tags_to_remove:
            return
        filtered_tags = [tag for tag in self.tags if tag not in tags_to_remove]
        if filtered_tags == self.tags:
            return
        self.beginResetModel()
        self.tags = filtered_tags
        self._tags_set = set(self.tags)
        self.endResetModel()
        aliases_changed, implications_changed = (
            self._cleanup_relationships_for_removed_tags(tags_to_remove))
        # _prune_orphaned_category_assignments() saves all library data,
        # persisting the alias/implication cleanup above as well.
        self._prune_orphaned_category_assignments()
        if aliases_changed:
            self.aliases_changed.emit()
        if implications_changed:
            self.implications_changed.emit()

    @Slot(str, str)
    def add_category(self, name: str, color: str):
        normalized_name = name.strip()
        normalized_color = self._normalize_color(color)
        if not normalized_name:
            return
        if any(category['name'].casefold() == normalized_name.casefold()
               for category in self.categories):
            return
        self.categories.append({
            'id': str(uuid4()),
            'name': normalized_name,
            'color': normalized_color
        })
        self._notify_model_changed()
        self.categories_changed.emit()
        self._save_all_tag_library_data()

    @Slot(str, str, str)
    def edit_category(self, category_id: str, name: str, color: str):
        normalized_name = name.strip()
        normalized_color = self._normalize_color(color)
        if not category_id or not normalized_name:
            return
        for category in self.categories:
            if category['id'] == category_id:
                if any(other_category['id'] != category_id
                       and other_category['name'].casefold()
                       == normalized_name.casefold()
                       for other_category in self.categories):
                    return
                category['name'] = normalized_name
                category['color'] = normalized_color
                self._notify_model_changed()
                self.categories_changed.emit()
                self._save_all_tag_library_data()
                return

    @Slot(str)
    def remove_category(self, category_id: str):
        if not category_id:
            return
        filtered_categories = [category for category in self.categories
                               if category['id'] != category_id]
        if len(filtered_categories) == len(self.categories):
            return
        self.categories = filtered_categories
        self.category_by_tag = {
            tag: assigned_category_id
            for tag, assigned_category_id in self.category_by_tag.items()
            if assigned_category_id != category_id
        }
        self._notify_model_changed()
        self.categories_changed.emit()
        self._save_all_tag_library_data()

    @Slot(list, str)
    def assign_category(self, tags: list[str], category_id: str):
        if not tags or not category_id:
            return
        category_ids = {category['id'] for category in self.categories}
        if category_id not in category_ids:
            return
        did_change = False
        for tag in tags:
            normalized_tag = tag.strip()
            if normalized_tag not in self._tags_set:
                continue
            if self.category_by_tag.get(normalized_tag) == category_id:
                continue
            self.category_by_tag[normalized_tag] = category_id
            did_change = True
        if not did_change:
            return
        self._notify_model_changed()
        self._save_all_tag_library_data()

    @Slot(list)
    def clear_category(self, tags: list[str]):
        if not tags:
            return
        did_change = False
        for tag in tags:
            normalized_tag = tag.strip()
            if normalized_tag not in self.category_by_tag:
                continue
            self.category_by_tag.pop(normalized_tag)
            did_change = True
        if not did_change:
            return
        self._notify_model_changed()
        self._save_all_tag_library_data()

    @Slot(list, str)
    def rename_tags(self, old_tags: list[str], new_tag: str):
        normalized_new_tag = new_tag.strip()
        if not old_tags or not normalized_new_tag:
            return
        normalized_old_tags = []
        seen_tags = set()
        for old_tag in old_tags:
            normalized_old_tag = old_tag.strip()
            if (not normalized_old_tag
                    or normalized_old_tag in seen_tags
                    or normalized_old_tag not in self._tags_set):
                continue
            normalized_old_tags.append(normalized_old_tag)
            seen_tags.add(normalized_old_tag)
        if not normalized_old_tags:
            return
        old_tags_set = set(normalized_old_tags)
        preferred_category_id = self.category_by_tag.get(normalized_new_tag)
        if not preferred_category_id:
            for old_tag in normalized_old_tags:
                preferred_category_id = self.category_by_tag.get(old_tag)
                if preferred_category_id:
                    break
        renamed_tags = [
            normalized_new_tag if tag in old_tags_set else tag
            for tag in self.tags
        ]
        renamed_tags = self._normalize_tags(renamed_tags)
        self.beginResetModel()
        self.tags = renamed_tags
        self._tags_set = set(self.tags)
        self.endResetModel()
        for old_tag in normalized_old_tags:
            self.category_by_tag.pop(old_tag, None)
        if preferred_category_id and normalized_new_tag in self.tags:
            self.category_by_tag[normalized_new_tag] = preferred_category_id
        aliases_changed, implications_changed = (
            self._rename_tag_in_relationships(old_tags_set, normalized_new_tag))
        # _prune_orphaned_category_assignments() saves all library data,
        # persisting the alias/implication updates above as well.
        self._prune_orphaned_category_assignments()
        if aliases_changed:
            self.aliases_changed.emit()
        if implications_changed:
            self.implications_changed.emit()

    def load_all_data(self, tags: list[str], categories: list[dict],
                      category_by_tag: dict[str, str], *,
                      aliases: dict[str, str] | None = None,
                      implications: dict[str, list[str]] | None = None,
                      profiles: dict[str, dict[str, str]] | None = None):
        """Atomically replace all tag library data (used for import)."""
        normalized = self._normalize_tags(tags)
        self.tags = normalized
        self._tags_set = set(self.tags)
        tag_set = set(normalized)

        self.categories = []
        for cat in categories:
            name = str(cat.get('name', '')).strip()
            if name:
                self.categories.append({
                    'id': str(cat.get('id', str(uuid4()))),
                    'name': name,
                    'color': self._normalize_color(str(cat.get('color', '')))
                })
        cat_ids = {c['id'] for c in self.categories}

        self.category_by_tag = {
            tag: cat_id for tag, cat_id in category_by_tag.items()
            if tag in tag_set and cat_id in cat_ids
        }
        self.aliases = aliases if aliases is not None else {}
        self.implications = implications if implications is not None else {}
        self.profiles = profiles if profiles is not None else {}

        self._save_all_tag_library_data()
        self._notify_model_changed()
        self.aliases_changed.emit()
        self.categories_changed.emit()
        self.implications_changed.emit()
        self.profiles_changed.emit()
        self.new_tags_added.emit(list(self.tags))

    @Slot(list)
    def set_category_order(self, ordered_category_ids: list[str]):
        if not ordered_category_ids:
            return
        category_by_id = {
            category['id']: category.copy()
            for category in self.categories
        }
        reordered_categories = []
        seen_category_ids = set()
        for category_id in ordered_category_ids:
            if category_id in seen_category_ids or category_id not in category_by_id:
                continue
            reordered_categories.append(category_by_id[category_id])
            seen_category_ids.add(category_id)
        for category in self.categories:
            if category['id'] in seen_category_ids:
                continue
            reordered_categories.append(category.copy())
        if reordered_categories == self.categories:
            return
        self.categories = reordered_categories
        self._notify_model_changed()
        self.categories_changed.emit()
        self._save_all_tag_library_data()
