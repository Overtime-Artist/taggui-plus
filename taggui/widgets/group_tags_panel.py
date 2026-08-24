"""Grouped Common/Partial tags view for multi-image (variant) selections.

When two or more images are selected, the Image Tags pane switches to this
panel instead of the single-image tag list. It shows:

* **Common tags** — tags present on *every* selected image (the shared base).
  Collapsible so it can be folded down to a one-line summary.
* **Differences** — tags present on *some* of the selected images, each with a
  ``k/N`` badge showing how many of the N selected images have it.

The panel only computes and displays the aggregate; the actual edits are
performed by the owning editor/model via the emitted signals, so all changes go
through the normal undo stack.
"""

from PySide6.QtCore import (QAbstractListModel, QItemSelectionModel,
                            QModelIndex, QSize, Qt, QTimer, Signal, Slot)
from PySide6.QtGui import QColor, QKeyEvent, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QListView,
                               QMenu, QLabel, QPushButton, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)

from models.tag_library_model import TagLibraryModel
from utils.image import Image
from utils.settings import get_tag_separator

# Custom roles used by the partial-tags model to carry the per-tag count and
# the total number of selected images through to the delegate.
COUNT_ROLE = Qt.ItemDataRole.UserRole + 1
TOTAL_ROLE = Qt.ItemDataRole.UserRole + 2


def compute_common_and_partial(
        images: list[Image]) -> tuple[list[str], list[tuple[str, int]], int]:
    """Return ``(common, partial, total)`` for a list of images.

    ``common`` is the tags present on every image (order of first appearance).
    ``partial`` is ``(tag, count)`` pairs for tags on some-but-not-all images,
    also in order of first appearance (consistent with ``common`` and the
    normal Image Tags list, and stable when a tag's count changes). Each row's
    ``k/N`` badge conveys the frequency instead. ``total`` is the number of
    images.
    """
    total = len(images)
    counts: dict[str, int] = {}
    order: list[str] = []
    for image in images:
        for tag in dict.fromkeys(image.tags):  # de-duplicate within an image
            if tag not in counts:
                counts[tag] = 0
                order.append(tag)
            counts[tag] += 1
    common = [tag for tag in order if counts[tag] == total]
    partial = [(tag, counts[tag]) for tag in order
               if 0 < counts[tag] < total]
    return common, partial, total


class _CommonTagModel(QAbstractListModel):
    """List of the common tags, colored by tag category. Editable (rename)."""

    rename_requested = Signal(str, str)

    def __init__(self, tag_library_model: TagLibraryModel):
        super().__init__()
        self.tag_library_model = tag_library_model
        self._tags: list[str] = []

    def set_tags(self, tags: list[str]):
        self.beginResetModel()
        self._tags = list(tags)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._tags)

    def data(self, index: QModelIndex,
             role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        tag = self._tags[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return tag
        if role == Qt.ItemDataRole.ForegroundRole:
            category = self.tag_library_model.get_category_for_tag(tag)
            if category:
                color = QColor(category['color'])
                if color.isValid():
                    return color
        return None

    def setData(self, index: QModelIndex, value,
                role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        old_tag = self._tags[index.row()]
        new_tag = str(value).strip()
        if new_tag and new_tag != old_tag:
            # The actual rename resets this model, so it is deferred by the
            # panel until the in-place editor has closed. Return False so the
            # view does not try to update the (about-to-be-reset) row itself.
            self.rename_requested.emit(old_tag, new_tag)
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEditable)


class _PartialTagModel(QAbstractListModel):
    """List of ``(tag, count)`` differences, colored by category. Editable."""

    rename_requested = Signal(str, str)

    def __init__(self, tag_library_model: TagLibraryModel):
        super().__init__()
        self.tag_library_model = tag_library_model
        self._rows: list[tuple[str, int]] = []
        self._total = 0

    def set_rows(self, rows: list[tuple[str, int]], total: int):
        self.beginResetModel()
        self._rows = list(rows)
        self._total = total
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex,
             role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        tag, count = self._rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return tag
        if role == COUNT_ROLE:
            return count
        if role == TOTAL_ROLE:
            return self._total
        if role == Qt.ItemDataRole.ForegroundRole:
            category = self.tag_library_model.get_category_for_tag(tag)
            if category:
                color = QColor(category['color'])
                if color.isValid():
                    return color
        return None

    def setData(self, index: QModelIndex, value,
                role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        old_tag = self._rows[index.row()][0]
        new_tag = str(value).strip()
        if new_tag and new_tag != old_tag:
            self.rename_requested.emit(old_tag, new_tag)
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEditable)


class _UniformHeightDelegate(QStyledItemDelegate):
    """Item delegate whose row height depends only on the font.

    Used for both tag lists so their vertical spacing matches. (The default
    delegate and a custom-painted delegate can otherwise pick slightly
    different row heights.)
    """

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex) -> QSize:
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        size = super().sizeHint(option, index)
        height = max(size.height(), option.fontMetrics.height() + 6)
        return QSize(size.width(), height)


class _PartialTagDelegate(_UniformHeightDelegate):
    """Draws a partial tag on the left and its ``k/N`` badge on the right."""

    def paint(self, painter, option: QStyleOptionViewItem,
              index: QModelIndex):
        painter.save()
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        # Paint the (possibly selected) background using the current style, but
        # without its default text so we can lay out the tag and badge.
        option.text = ''
        style = (option.widget.style() if option.widget
                 else QApplication.style())
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option,
                          painter, option.widget)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            text_color = option.palette.color(
                QPalette.ColorRole.HighlightedText)
        else:
            foreground = index.data(Qt.ItemDataRole.ForegroundRole)
            text_color = (foreground if isinstance(foreground, QColor)
                          else option.palette.color(QPalette.ColorRole.Text))
        badge_color = QColor(text_color)
        badge_color.setAlpha(150)

        count = index.data(COUNT_ROLE)
        total = index.data(TOTAL_ROLE)
        badge = f'{count}/{total}'
        metrics = option.fontMetrics
        badge_width = metrics.horizontalAdvance(badge) + 8
        content_rect = option.rect.adjusted(8, 0, -8, 0)

        painter.setPen(badge_color)
        painter.drawText(content_rect,
                         int(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter),
                         badge)

        tag = index.data(Qt.ItemDataRole.DisplayRole)
        text_rect = content_rect.adjusted(0, 0, -badge_width, 0)
        elided = metrics.elidedText(tag, Qt.TextElideMode.ElideRight,
                                    text_rect.width())
        painter.setPen(text_color)
        painter.drawText(text_rect,
                         int(Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter),
                         elided)
        painter.restore()


class _TagListView(QListView):
    """List view whose Delete/Backspace asks the panel to remove the selection.

    Left/Right arrows are ignored so they bubble up to the Images pane, keeping
    the "arrow through the selected variants" navigation working while focus is
    in this panel. Up/Down navigate the tags; at the top/bottom edge they ask
    the panel to move to the previous/next selected image instead (so the
    multi-image selection is never lost). Ctrl+Up/Down asks to select an image
    outside the selection.
    """

    delete_requested = Signal()
    # Emitted with -1 (Up at the top) or 1 (Down at the bottom) when the user
    # tries to navigate past the edge of this list.
    edge_navigation = Signal(int)
    # Emitted with -1/1 for Ctrl+Up / Ctrl+Down (select beyond the selection).
    escape_navigation = Signal(int)

    def __init__(self):
        super().__init__()
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setWordWrap(True)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._syncing_selection_colors = False
        self._sync_inactive_selection_colors()

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == event.Type.PaletteChange
                and not self._syncing_selection_colors):
            self._syncing_selection_colors = True
            self._sync_inactive_selection_colors()
            self._syncing_selection_colors = False

    def _sync_inactive_selection_colors(self):
        palette = self.palette()
        for role in (QPalette.ColorRole.Highlight,
                     QPalette.ColorRole.HighlightedText):
            active_color = palette.color(QPalette.ColorGroup.Active, role)
            palette.setColor(QPalette.ColorGroup.Inactive, role, active_color)
        self.setPalette(palette)

    def _at_edge(self, direction: int) -> bool:
        """Whether the current tag is at the top (dir<0) or bottom (dir>0)."""
        model = self.model()
        count = model.rowCount() if model is not None else 0
        if count == 0:
            return True
        row = self.currentIndex().row()
        if row < 0:
            # Nothing is focused yet; let the default navigation pick a tag.
            return False
        return row <= 0 if direction < 0 else row >= count - 1

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.selectedIndexes():
                self.delete_requested.emit()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            event.ignore()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            direction = -1 if key == Qt.Key.Key_Up else 1
            control_pressed = bool(
                modifiers & Qt.KeyboardModifier.ControlModifier)
            shift_pressed = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            if control_pressed and not shift_pressed:
                self.escape_navigation.emit(direction)
                return
            if modifiers == Qt.KeyboardModifier.NoModifier:
                if self._at_edge(direction):
                    self.edge_navigation.emit(direction)
                    return
                super().keyPressEvent(event)
                return
        super().keyPressEvent(event)

    def selected_tags(self) -> list[str]:
        return [str(index.data(Qt.ItemDataRole.DisplayRole))
                for index in self.selectedIndexes()
                if index.data(Qt.ItemDataRole.DisplayRole)]

    def copy_selected_tags_to_clipboard(self) -> bool:
        """Copy this list's selected tags, matching the normal Image Tags list.

        The window-wide "Copy Tags" (Ctrl+C) shortcut walks up from the focused
        widget looking for this method; returning True tells it the copy was
        handled here instead of copying every tag of every selected image.
        """
        tags = self.selected_tags()
        if not tags:
            return False
        QApplication.clipboard().setText(get_tag_separator().join(tags))
        return True


class GroupTagsPanel(QWidget):
    """The Common/Differences panel shown for multi-image selections."""

    # Remove the given tags from every selected image.
    remove_from_all_requested = Signal(list)
    # Add the given tags to every selected image (promote a difference to
    # common).
    add_to_all_requested = Signal(list)
    # Remove the given tags from just the current (highlighted) image.
    remove_from_current_requested = Signal(list)
    # Add the given tags to just the current (highlighted) image.
    add_to_current_requested = Signal(list)
    # The set of currently-selected Differences tags changed. Carries the list
    # of selected partial tags (empty when nothing is selected) so the grid can
    # highlight which images contain them.
    partial_focus_changed = Signal(list)
    # Move the current image to the previous (-1) / next (1) selected image,
    # requested when arrowing past the top/bottom of a tag list.
    cycle_image_requested = Signal(int)
    # Select an image outside the current selection (Ctrl+Up / Ctrl+Down).
    escape_selection_requested = Signal(int)
    # Look up a tag in the Danbooru / Gelbooru wiki (right-click a tag).
    danbooru_wiki_requested = Signal(str)
    gelbooru_wiki_requested = Signal(str)
    # Rename a tag (old, new) on the selected images that contain it.
    rename_tag_requested = Signal(str, str)

    def __init__(self, tag_library_model: TagLibraryModel):
        super().__init__()
        self.tag_library_model = tag_library_model
        self._common_model = _CommonTagModel(tag_library_model)
        self._partial_model = _PartialTagModel(tag_library_model)
        self._common_collapsed = False
        # Row to reselect after the next model refresh, so deleting/editing a
        # tag from a list keeps the cursor at the same position instead of
        # resetting to the top (mirrors the normal Image Tags list). One-shot:
        # set right before an edit is requested, consumed by the next refresh.
        self._pending_common_anchor: int | None = None
        self._pending_partial_anchor: int | None = None
        # Persistent display order for the Common/Differences lists. Tags keep
        # their slot across edits to the same selection so that, e.g., removing
        # a tag from the current image changes only its k/N badge, not its
        # position. `_order_key` identifies the current selection (by image
        # paths); when it changes the remembered order is rebuilt from the
        # natural first-appearance order. See `_apply_stable_order`.
        self._order_key: frozenset[str] | None = None
        self._common_order: list[str] = []
        self._partial_order: list[str] = []

        self.common_header = QPushButton()
        self.common_header.setCheckable(True)
        self.common_header.setChecked(True)
        self.common_header.setFlat(True)
        self.common_header.setStyleSheet('QPushButton { text-align: left; }')
        self.common_header.setAutoDefault(False)
        self.common_header.setDefault(False)
        self.common_header.toggled.connect(self._on_common_header_toggled)

        self.common_list = _TagListView()
        self.common_list.setModel(self._common_model)
        self.common_list.setItemDelegate(_UniformHeightDelegate(self))
        self.common_list.delete_requested.connect(
            self._remove_selected_common)
        self.common_list.customContextMenuRequested.connect(
            self._show_common_context_menu)
        self.common_list.edge_navigation.connect(self._on_common_edge)
        self.common_list.escape_navigation.connect(
            self.escape_selection_requested)
        self._common_model.rename_requested.connect(
            lambda old, new: self._on_rename_requested(
                self.common_list, old, new))

        self.partial_label = QLabel()
        self.partial_list = _TagListView()
        self.partial_list.setModel(self._partial_model)
        self.partial_list.setItemDelegate(_PartialTagDelegate(self))
        self.partial_list.delete_requested.connect(
            self._remove_selected_partial)
        self.partial_list.customContextMenuRequested.connect(
            self._show_partial_context_menu)
        self.partial_list.selectionModel().selectionChanged.connect(
            self._on_partial_selection_changed)
        self.partial_list.edge_navigation.connect(self._on_partial_edge)
        self.partial_list.escape_navigation.connect(
            self.escape_selection_requested)
        self._partial_model.rename_requested.connect(
            lambda old, new: self._on_rename_requested(
                self.partial_list, old, new))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.common_header)
        layout.addWidget(self.common_list)
        layout.addWidget(self.partial_label)
        layout.addWidget(self.partial_list)
        # The common tags are the shared base and tend to be the longer list,
        # so give that list the larger share of the vertical space when the
        # pane grows. The differences list still gets a meaningful minimum.
        layout.setStretchFactor(self.common_list, 3)
        layout.setStretchFactor(self.partial_list, 2)

        self._refresh_labels(0, 0)

    def set_images(self, images: list[Image], restore_positions: bool = False):
        common, partial, total = compute_common_and_partial(images)
        # Keep each tag in a stable slot across edits to the same selection.
        # When the selected image set changes, rebuild the order from the
        # natural first-appearance order returned above.
        order_key = frozenset(str(image.path) for image in images)
        same_selection = order_key == self._order_key
        if not same_selection:
            self._order_key = order_key
            self._common_order = []
            self._partial_order = []
        # When an external edit (auto-captioning, undo/redo) changes the tags of
        # the same selection without the panel setting an explicit row anchor,
        # remember the selected tags by name so they stay selected across the
        # model reset below, mirroring the normal Image Tags list. Panel edits
        # (delete/rename/add) instead restore the cursor by row via the pending
        # anchors, so skip name-based preservation for those.
        preserve_common = same_selection and self._pending_common_anchor is None
        preserve_partial = (same_selection
                            and self._pending_partial_anchor is None)
        prev_common_tags = (self.common_list.selected_tags()
                            if preserve_common else [])
        prev_partial_tags = (self.partial_list.selected_tags()
                             if preserve_partial else [])
        # A genuinely new tag (manual add or auto-caption) lands at the end of
        # the list, matching the normal Image Tags list. When restoring from
        # the undo/redo history we instead keep the remembered order intact:
        # vanished tags are not pruned and a reappearing tag reclaims its exact
        # prior slot, so an add -> undo -> redo cycle returns the tag to where
        # it was rather than to its natural first-appearance position.
        place_new_at_end = not restore_positions
        prune_absent = not restore_positions
        common = self._apply_stable_order(common, self._common_order,
                                          place_new_at_end, prune_absent)
        partial_counts = dict(partial)
        partial_tags = self._apply_stable_order(
            [tag for tag, _ in partial], self._partial_order, place_new_at_end,
            prune_absent)
        partial = [(tag, partial_counts[tag]) for tag in partial_tags]
        self._common_model.set_tags(common)
        self._partial_model.set_rows(partial, total)
        self._refresh_labels(len(common), len(partial))
        # Resetting the models above clears any selection. If an edit was just
        # made from one of the lists, restore the cursor to the same row so the
        # position is preserved (consistent with the normal Image Tags list).
        self._restore_anchor(self.common_list, self._common_model,
                             self._pending_common_anchor)
        self._pending_common_anchor = None
        self._restore_anchor(self.partial_list, self._partial_model,
                             self._pending_partial_anchor)
        self._pending_partial_anchor = None
        # For external edits, reselect the previously-selected tags by name so
        # the user's selection is maintained (e.g. after auto-captioning).
        if prev_common_tags:
            self._reselect_tags(self.common_list, common, prev_common_tags)
        if prev_partial_tags:
            self._reselect_tags(self.partial_list, partial_tags,
                                prev_partial_tags)
        # Report the (possibly restored) Differences selection so the grid
        # highlight stays in sync.
        self.partial_focus_changed.emit(self.partial_list.selected_tags())

    @staticmethod
    def _apply_stable_order(current_tags: list[str],
                            remembered: list[str],
                            place_new_at_end: bool = True,
                            prune_absent: bool = True) -> list[str]:
        """Order ``current_tags`` by their remembered slots, updating in place.

        Returns the display order (present tags only). Tags already in
        ``remembered`` keep their relative position, so an edit that only
        changes a tag's ``k/N`` count doesn't reshuffle the list. A tag not seen
        before is placed according to ``place_new_at_end``:

        - ``True`` (the default): appended at the end, so a freshly added tag
          (manual add or auto-caption) lands at the bottom of the list, exactly
          like the normal Image Tags list.
        - ``False``: inserted at the position it occupies in ``current_tags``
          (its natural first-appearance order) relative to the remembered tags.

        ``prune_absent`` controls how ``remembered`` is updated:

        - ``True`` (the default): ``remembered`` is replaced with the display
          order, so tags no longer present drop out. Used for ordinary edits, so
          re-adding a previously deleted tag treats it as new (goes to the end).
        - ``False``: tags no longer present are kept in ``remembered`` at their
          existing slots while present tags are reordered to the display order.
          Used when restoring from the undo/redo history, so a tag that vanishes
          on undo reclaims its exact prior slot on redo (an add -> undo -> redo
          cycle returns the tag to where it was, not to its natural position).

        Because a rename keeps the tag in ``remembered`` (the panel swaps the
        name in place), a renamed tag also keeps its position.
        """
        current_set = set(current_tags)
        natural_index = {tag: position
                         for position, tag in enumerate(current_tags)}
        ordered = [tag for tag in remembered if tag in current_set]
        seen = set(ordered)
        for tag in current_tags:
            if tag in seen:
                continue
            if place_new_at_end:
                insert_at = len(ordered)
            else:
                # Insert before the first already-placed tag that comes after
                # this one in the natural order; otherwise append at the end.
                insert_at = len(ordered)
                for position, placed_tag in enumerate(ordered):
                    if natural_index[placed_tag] > natural_index[tag]:
                        insert_at = position
                        break
            ordered.insert(insert_at, tag)
            seen.add(tag)
        if prune_absent:
            remembered[:] = ordered
        else:
            # Rebuild the memory keeping absent tags anchored at their slots and
            # substituting present tags in their new display order.
            display_iter = iter(ordered)
            rebuilt = []
            for tag in remembered:
                if tag in current_set:
                    rebuilt.append(next(display_iter))
                else:
                    rebuilt.append(tag)
            rebuilt.extend(display_iter)
            remembered[:] = rebuilt
        return ordered

    @staticmethod
    def _anchor_row(list_view: '_TagListView') -> int | None:
        """The topmost selected row in a list, used as the restore position."""
        rows = [index.row() for index in list_view.selectedIndexes()]
        return min(rows) if rows else None

    def remember_anchor_for_add(self, list_view: '_TagListView'):
        """Stash ``list_view``'s selected row so it is restored on the next
        refresh.

        Used when typing on a Common/Differences list auto-focuses the Add Tag
        box: after the tag is added (which resets the models and clears the
        selection), the previously selected tag is reselected, matching the
        normal Image Tags list.
        """
        if list_view is self.common_list:
            self._pending_common_anchor = self._anchor_row(self.common_list)
        elif list_view is self.partial_list:
            self._pending_partial_anchor = self._anchor_row(self.partial_list)

    @staticmethod
    def _restore_anchor(list_view: '_TagListView',
                        model: QAbstractListModel, anchor: int | None):
        """Reselect the tag now at ``anchor`` after a refresh.

        Matches the normal Image Tags list: select the row that took the
        deleted row's place, or the last row if the list got shorter.
        """
        if anchor is None:
            return
        count = model.rowCount()
        if count == 0:
            return
        row = anchor if anchor < count else count - 1
        index = model.index(row)
        list_view.setCurrentIndex(index)
        list_view.selectionModel().select(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    @staticmethod
    def _reselect_tags(list_view: '_TagListView', ordered_tags: list[str],
                       wanted_tags: list[str]):
        """Reselect the given tags by name after a refresh.

        Used to preserve the user's selection across an external tag change
        (auto-captioning, undo/redo) that resets the models: any of
        ``wanted_tags`` still present is reselected at its new row, mirroring the
        normal Image Tags list keeping its selection. Tags that no longer exist
        are ignored.
        """
        row_by_tag = {tag: row for row, tag in enumerate(ordered_tags)}
        rows = sorted(row_by_tag[tag] for tag in wanted_tags
                      if tag in row_by_tag)
        if not rows:
            return
        model = list_view.model()
        selection_model = list_view.selectionModel()
        selection_model.clearSelection()
        for row in rows:
            selection_model.select(
                model.index(row), QItemSelectionModel.SelectionFlag.Select)
        list_view.setCurrentIndex(model.index(rows[0]))

    @Slot()
    def _on_partial_selection_changed(self, *args):
        self.partial_focus_changed.emit(self.partial_list.selected_tags())

    def focus_first_tag(self) -> bool:
        """Focus the first available tag list and select its first tag.

        Prefers the Common list (when expanded and non-empty), otherwise the
        Differences list. Used by the "Focus Image Tags List" shortcut while the
        grouped view is showing. Returns False when there is no tag to focus.
        """
        if self._focus_list_edge(self.common_list, self._common_model,
                                 at_top=True):
            return True
        return self._focus_list_edge(self.partial_list, self._partial_model,
                                     at_top=True)

    def _focus_list_edge(self, list_view: '_TagListView',
                         model: QAbstractListModel, at_top: bool) -> bool:
        """Move keyboard focus/selection to the top or bottom of a tag list.

        Returns False (so the caller can fall back to cycling images) when the
        target list is hidden or empty.
        """
        if not list_view.isVisible() or model.rowCount() == 0:
            return False
        row = 0 if at_top else model.rowCount() - 1
        index = model.index(row)
        list_view.setFocus()
        list_view.setCurrentIndex(index)
        list_view.selectionModel().select(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        return True

    @Slot(int)
    def _on_common_edge(self, direction: int):
        # Up past the top of Common leaves the tag lists entirely -> prev image.
        # Down past the bottom chains into Differences (if any) before images.
        if direction > 0 and self._focus_list_edge(
                self.partial_list, self._partial_model, at_top=True):
            return
        self.cycle_image_requested.emit(direction)

    @Slot(int)
    def _on_partial_edge(self, direction: int):
        # Up past the top of Differences chains back into Common (if visible);
        # Down past the bottom leaves the tag lists entirely -> next image.
        if direction < 0 and self._focus_list_edge(
                self.common_list, self._common_model, at_top=False):
            return
        self.cycle_image_requested.emit(direction)

    def _refresh_labels(self, common_count: int, partial_count: int):
        arrow = '\u25b8' if self._common_collapsed else '\u25be'
        self.common_header.setText(
            f'{arrow} Common tags ({common_count})')
        self.partial_label.setText(f'Differences ({partial_count})')

    @Slot(bool)
    def _on_common_header_toggled(self, checked: bool):
        self._common_collapsed = not checked
        self.common_list.setVisible(checked)
        self._refresh_labels(self._common_model.rowCount(),
                             self._partial_model.rowCount())

    @Slot()
    def _remove_selected_common(self):
        tags = self.common_list.selected_tags()
        if tags:
            self._pending_common_anchor = self._anchor_row(self.common_list)
            self.remove_from_all_requested.emit(tags)
            self._pending_common_anchor = None

    @Slot()
    def _remove_selected_partial(self):
        tags = self.partial_list.selected_tags()
        if tags:
            self._pending_partial_anchor = self._anchor_row(self.partial_list)
            self.remove_from_all_requested.emit(tags)
            self._pending_partial_anchor = None

    @staticmethod
    def _tag_at(list_view: '_TagListView', position) -> str:
        """The tag under the given viewport position, or '' if none."""
        index = list_view.indexAt(position)
        if not index.isValid():
            return ''
        data = index.data(Qt.ItemDataRole.DisplayRole)
        return str(data) if data else ''

    def selected_tag_for_wiki(self) -> str:
        """The single tag the wiki shortcut should look up from this panel.

        Mirrors ImageTagsList.selected_tag_for_wiki: returns a tag only when
        exactly one is selected, otherwise ''. If one of the lists has keyboard
        focus, that list alone decides (so multi-selection there yields '',
        matching the normal list). When neither list has focus (e.g. the Add Tag
        box is focused), fall back to whichever list has a single selection,
        preferring Common.
        """
        for list_view in (self.common_list, self.partial_list):
            if list_view.hasFocus():
                tags = list_view.selected_tags()
                return tags[0] if len(tags) == 1 else ''
        for list_view in (self.common_list, self.partial_list):
            tags = list_view.selected_tags()
            if len(tags) == 1:
                return tags[0]
        return ''

    def _copy_tags(self, list_view: '_TagListView'):
        list_view.copy_selected_tags_to_clipboard()

    def _add_category_actions(self, menu: QMenu, tag: str) -> tuple[dict, object]:
        """Append an "Assign Category" submenu and a "Clear Category" action.

        Mirrors ImageTagsList.show_context_menu. Returns the mapping of assign
        actions to category ids and the clear action so the caller can dispatch.
        """
        categories = self.tag_library_model.get_categories()
        assign_menu = menu.addMenu('Assign Category')
        category_actions = {}
        for category in categories:
            action = assign_menu.addAction(category['name'])
            category_actions[action] = category['id']
        assign_menu.setEnabled(bool(categories))
        clear_action = menu.addAction('Clear Category')
        clear_action.setEnabled(
            self.tag_library_model.get_category_for_tag(tag) is not None)
        return category_actions, clear_action

    def _assign_category(self, tag: str, category_id):
        if not self.tag_library_model.has_tag(tag):
            self.tag_library_model.add_tags([tag])
        self.tag_library_model.assign_category([tag], category_id)
        self._refresh_tag_colors()

    def _clear_category(self, tag: str):
        self.tag_library_model.clear_category([tag])
        self._refresh_tag_colors()

    def _refresh_tag_colors(self):
        """Repaint both lists so category color changes take effect."""
        self.common_list.viewport().update()
        self.partial_list.viewport().update()

    def _on_rename_requested(self, list_view: '_TagListView',
                             old_tag: str, new_tag: str):
        """Defer an in-place rename until the item editor has fully closed.

        Performing the rename synchronously inside the model's setData would
        reset the model while its editor is still closing, which can crash. So
        we stash the cursor position and fire the rename on the next event-loop
        tick, when the editor is gone.
        """
        is_common = list_view is self.common_list
        anchor = self._anchor_row(list_view)
        QTimer.singleShot(
            0, lambda: self._emit_rename(is_common, anchor, old_tag, new_tag))

    def _emit_rename(self, is_common: bool, anchor: int | None,
                     old_tag: str, new_tag: str):
        # Keep the renamed tag in its slot. New tags default to the end of the
        # list, so a rename must swap the name in the remembered order in place
        # (rather than letting the new name be treated as a brand-new tag and
        # appended). The pending anchor then restores the cursor to that row.
        self._rename_in_remembered_order(self._common_order, old_tag, new_tag)
        self._rename_in_remembered_order(self._partial_order, old_tag, new_tag)
        if is_common:
            self._pending_common_anchor = anchor
        else:
            self._pending_partial_anchor = anchor
        self.rename_tag_requested.emit(old_tag, new_tag)
        self._pending_common_anchor = None
        self._pending_partial_anchor = None

    @staticmethod
    def _rename_in_remembered_order(remembered: list[str], old_tag: str,
                                    new_tag: str):
        """Replace ``old_tag`` with ``new_tag`` in a remembered order list.

        If ``new_tag`` already exists (the rename merges into an existing tag),
        just drop ``old_tag`` so the merged tag keeps its own slot.
        """
        if old_tag not in remembered:
            return
        index = remembered.index(old_tag)
        if new_tag in remembered:
            remembered.pop(index)
        else:
            remembered[index] = new_tag

    def _show_common_context_menu(self, position):
        tags = self.common_list.selected_tags()
        if not tags:
            return
        clicked_tag = self._tag_at(self.common_list, position)
        menu = QMenu(self)
        copy_action = menu.addAction(
            'Copy Tags' if len(tags) > 1 else 'Copy Tag')
        menu.addSeparator()
        remove_all_action = menu.addAction('Remove from all selected')
        remove_current_action = menu.addAction('Remove from current image')
        view_danbooru_action = None
        view_gelbooru_action = None
        category_actions = {}
        clear_action = None
        if clicked_tag:
            menu.addSeparator()
            view_danbooru_action = menu.addAction('View Danbooru Wiki')
            view_gelbooru_action = menu.addAction('View Gelbooru Wiki')
            menu.addSeparator()
            category_actions, clear_action = self._add_category_actions(
                menu, clicked_tag)
        chosen = menu.exec(self.common_list.viewport().mapToGlobal(position))
        if chosen is None:
            return
        # These actions don't reorder the list, so don't touch the anchor.
        if chosen == copy_action:
            self._copy_tags(self.common_list)
            return
        if chosen == view_danbooru_action:
            self.danbooru_wiki_requested.emit(clicked_tag)
            return
        if chosen == view_gelbooru_action:
            self.gelbooru_wiki_requested.emit(clicked_tag)
            return
        if chosen == clear_action:
            self._clear_category(clicked_tag)
            return
        if chosen in category_actions:
            self._assign_category(clicked_tag, category_actions[chosen])
            return
        # Preserve the cursor position across the refresh the edit triggers.
        self._pending_common_anchor = self._anchor_row(self.common_list)
        if chosen == remove_all_action:
            self.remove_from_all_requested.emit(tags)
        elif chosen == remove_current_action:
            self.remove_from_current_requested.emit(tags)
        # Clear if the edit was a no-op (no refresh consumed the anchor).
        self._pending_common_anchor = None

    def _show_partial_context_menu(self, position):
        tags = self.partial_list.selected_tags()
        if not tags:
            return
        clicked_tag = self._tag_at(self.partial_list, position)
        menu = QMenu(self)
        copy_action = menu.addAction(
            'Copy Tags' if len(tags) > 1 else 'Copy Tag')
        menu.addSeparator()
        add_all_action = menu.addAction('Add to all selected')
        add_current_action = menu.addAction('Add to current image')
        remove_all_action = menu.addAction('Remove from all selected')
        remove_current_action = menu.addAction('Remove from current image')
        view_danbooru_action = None
        view_gelbooru_action = None
        category_actions = {}
        clear_action = None
        if clicked_tag:
            menu.addSeparator()
            view_danbooru_action = menu.addAction('View Danbooru Wiki')
            view_gelbooru_action = menu.addAction('View Gelbooru Wiki')
            menu.addSeparator()
            category_actions, clear_action = self._add_category_actions(
                menu, clicked_tag)
        chosen = menu.exec(self.partial_list.viewport().mapToGlobal(position))
        if chosen is None:
            return
        # These actions don't reorder the list, so don't touch the anchor.
        if chosen == copy_action:
            self._copy_tags(self.partial_list)
            return
        if chosen == view_danbooru_action:
            self.danbooru_wiki_requested.emit(clicked_tag)
            return
        if chosen == view_gelbooru_action:
            self.gelbooru_wiki_requested.emit(clicked_tag)
            return
        if chosen == clear_action:
            self._clear_category(clicked_tag)
            return
        if chosen in category_actions:
            self._assign_category(clicked_tag, category_actions[chosen])
            return
        # Preserve the cursor position across the refresh the edit triggers.
        self._pending_partial_anchor = self._anchor_row(self.partial_list)
        if chosen == add_all_action:
            self.add_to_all_requested.emit(tags)
        elif chosen == add_current_action:
            self.add_to_current_requested.emit(tags)
        elif chosen == remove_all_action:
            self.remove_from_all_requested.emit(tags)
        elif chosen == remove_current_action:
            self.remove_from_current_requested.emit(tags)
        # Clear if the edit was a no-op (no refresh consumed the anchor).
        self._pending_partial_anchor = None
