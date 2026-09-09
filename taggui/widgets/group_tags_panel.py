"""Unified grouped tags view for multi-image (variant) selections.

When two or more images are selected, the Image Tags pane switches to this
panel instead of the single-image tag list. It shows a single flat list of
every tag across the selection, each with a ``k/N`` badge counting how many of
the N selected images have it (a *common* tag reads ``N/N``; a *difference*
reads ``k/N`` with ``k < N``). The list stays in a stable first-appearance
order that is frozen for the whole review, so cycling the current image never
reshuffles it.

A "Only differences" filter hides the common tags without reordering anything,
and selecting a difference tag drives the grid split via
``partial_focus_changed``.

The panel only computes and displays the aggregate; the actual edits are
performed by the owning editor/model via the emitted signals, so all changes go
through the normal undo stack.
"""

from PySide6.QtCore import (QAbstractListModel, QItemSelectionModel,
                            QModelIndex, QSize, Qt, QTimer, Signal, Slot)
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication,
                               QHBoxLayout,
                               QListView, QMenu, QLabel,
                               QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)

from models.tag_library_model import TagLibraryModel
from utils.image import Image
from utils.settings import DEFAULT_SETTINGS, get_settings, get_tag_separator

# Custom roles used by the tag model to carry the per-tag count and the total
# number of selected images through to the delegate.
COUNT_ROLE = Qt.ItemDataRole.UserRole + 1
TOTAL_ROLE = Qt.ItemDataRole.UserRole + 2


def compute_tag_rows(
        images: list[Image]) -> tuple[list[tuple[str, int]], int]:
    """Return ``(rows, total)`` for a list of images.

    ``rows`` is ``(tag, count)`` pairs for every tag present on any of the
    images, in order of first appearance across the selection (consistent with
    the normal Image Tags list, and stable when a tag's count changes). ``count``
    is how many of the images contain the tag, and ``total`` is the number of
    images, so a tag is *common* when ``count == total`` and a *difference*
    otherwise.
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
    rows = [(tag, counts[tag]) for tag in order]
    return rows, total


class _UnifiedTagModel(QAbstractListModel):
    """Flat list of ``(tag, count)`` rows, colored by category. Editable.

    Carries the per-tag count and the shared total through the count/total
    roles so the delegate can draw each row's ``k/N`` badge.
    """

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


class _UniformHeightDelegate(QStyledItemDelegate):
    """Item delegate whose row height matches the single-image Image Tags list.

    The single-image list uses ``TextEditItemDelegate``, whose ``sizeHint`` adds
    8 px to the default row height. This mirrors that exactly so the grouped
    (grid-view) tag list has identical vertical spacing; without it the custom
    delegate picks a slightly shorter row and the list looks denser.
    """

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), size.height() + 8)


class _TagBadgeDelegate(_UniformHeightDelegate):
    """Draws a tag on the left and its ``k/N`` badge on the right."""

    def paint(self, painter, option: QStyleOptionViewItem,
              index: QModelIndex):
        painter.save()
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        # Keep a selected row painted with the vivid "active" highlight even when
        # this list does not hold keyboard focus. Without this, losing focus
        # (e.g. after an add -> undo, when focus moves off the list) clears the
        # State_Active flag and the style falls back to the dim "inactive"
        # highlight, making the selection look greyed out. The Common list and
        # the main Image Tags list already appear vivid when unfocused; this
        # keeps the Differences list consistent with them.
        if option.state & QStyle.StateFlag.State_Selected:
            option.state |= QStyle.StateFlag.State_Active
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
        # Lay the text out in exactly the same place as the single-image Image
        # Tags list so the two lists align pixel-for-pixel. That list uses
        # TextEditItemDelegate, which inset the row by +4 and then lets the
        # style position the text (adding its own margin). Mirror both steps by
        # asking the style for the text sub-rect of the +4 inset row, instead of
        # drawing at a hard-coded offset (which omits the style margin).
        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(4, 0, 0, 0)
        # subElementRect gives the item's text area, but the style then insets
        # the text by a further `textMargin` (PM_FocusFrameHMargin + 1) on each
        # side when it actually draws it. The single-image list goes through the
        # style, so mirror that inset here; otherwise the grid text sits a few
        # pixels further left than the single-image list.
        text_margin = style.pixelMetric(
            QStyle.PixelMetric.PM_FocusFrameHMargin, option, option.widget) + 1
        content_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, text_option,
            option.widget).adjusted(text_margin, 0, -text_margin, 0)

        painter.setPen(badge_color)
        painter.drawText(content_rect,
                         int(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter),
                         badge)

        tag = index.data(Qt.ItemDataRole.DisplayRole)
        # Draw a subtle "review" marker for tags the most recent Auto-Captioner
        # run added or increased the count of, so the user can see at a glance
        # which existing tags were touched. The dot sits just left of the k/N
        # badge; when present it reserves a little extra width so it never
        # overlaps the (right-elided) tag text or the badge.
        panel = self.parent()
        marked = bool(panel is not None
                      and tag in getattr(panel, '_recently_captioned', ()))
        dot_reserve = 0
        if marked:
            dot_diameter = 6
            dot_gap = 5
            dot_reserve = dot_diameter + 2 * dot_gap
            dot_color = QColor(0xE0, 0xA0, 0x30)
            dot_right = content_rect.right() - badge_width - dot_gap
            dot_left = dot_right - dot_diameter
            dot_top = content_rect.center().y() - dot_diameter // 2 + 1
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot_color)
            painter.drawEllipse(dot_left, dot_top, dot_diameter, dot_diameter)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        text_rect = content_rect.adjusted(0, 0, -(badge_width + dot_reserve), 0)
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


class _ClickableLabel(QLabel):
    """A text label that acts like a link: it emits ``clicked`` when pressed,
    shows a pointing-hand cursor, and underlines on hover. Its text colour is
    driven by the parent (bright when active, muted when not) via ``set_color``;
    the look of the text otherwise never changes.
    """

    clicked = Signal()

    def __init__(self, text: str = ''):
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_color(self, color: QColor):
        self.setStyleSheet(f'color: {color.name()};')

    def _set_underline(self, on: bool):
        font = self.font()
        font.setUnderline(on)
        self.setFont(font)

    def enterEvent(self, event):
        self._set_underline(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_underline(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SummaryFilter(QWidget):
    """The ``N common · M differences`` status text, with each half clickable to
    filter the tag list. Clicking a half activates that filter (and brightens
    that half); clicking the active half again returns to showing all tags. The
    look is unchanged except that the active half is brightened and the inactive
    half dimmed.
    """

    # Emitted with the half that was clicked: 'common' or 'differences'.
    half_clicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.common_label = _ClickableLabel('0 common')
        self.dot_label = QLabel('\u00b7')
        self.diff_label = _ClickableLabel('0 differences')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.common_label)
        layout.addWidget(self.dot_label)
        layout.addWidget(self.diff_label)
        self.common_label.clicked.connect(
            lambda: self.half_clicked.emit('common'))
        self.diff_label.clicked.connect(
            lambda: self.half_clicked.emit('differences'))

    def set_counts(self, common: int, differences: int):
        self.common_label.setText(f'{common} common')
        self.diff_label.setText(f'{differences} differences')

    def set_active(self, mode: str):
        """Brighten the active half (mode 'common'/'differences') and dim the
        rest. 'all' dims both. Colours are read from the palette so this works
        in both light and dark themes."""
        palette = self.palette()
        active = palette.color(QPalette.ColorRole.WindowText)
        muted = palette.color(QPalette.ColorGroup.Disabled,
                              QPalette.ColorRole.WindowText)
        self.common_label.set_color(
            active if mode == 'common' else muted)
        self.diff_label.set_color(
            active if mode == 'differences' else muted)
        self.dot_label.setStyleSheet(f'color: {muted.name()};')


class GroupTagsPanel(QWidget):
    """The unified grouped-tags panel shown for multi-image selections."""

    # Remove the given tags from every selected image.
    remove_from_all_requested = Signal(list)
    # Add the given tags to every selected image (promote a difference to
    # common).
    add_to_all_requested = Signal(list)
    # Remove the given tags from just the current (highlighted) image.
    remove_from_current_requested = Signal(list)
    # Add the given tags to just the current (highlighted) image.
    add_to_current_requested = Signal(list)
    # The set of currently-selected *difference* tags changed. Carries the list
    # of selected tags that are not on every image (empty when nothing, or only
    # common tags, is selected) so the grid can split by them.
    partial_focus_changed = Signal(list)
    # Move the current image to the previous (-1) / next (1) selected image,
    # requested when arrowing past the top/bottom of the tag list.
    cycle_image_requested = Signal(int)
    # Select an image outside the current selection (Ctrl+Up / Ctrl+Down).
    escape_selection_requested = Signal(int)
    # Look up a tag in the Danbooru / Gelbooru wiki (right-click a tag).
    danbooru_wiki_requested = Signal(str)
    gelbooru_wiki_requested = Signal(str)
    # Rename a tag (old, new) on the selected images that contain it.
    rename_tag_requested = Signal(str, str)
    # The tag-list filter changed via the clickable summary. Carries the new
    # mode: 'all', 'common', or 'differences'. Used to drive the Add Tag scope
    # button (differences -> current-image-only, common -> all).
    filter_mode_changed = Signal(str)

    def __init__(self, tag_library_model: TagLibraryModel):
        super().__init__()
        self.tag_library_model = tag_library_model
        self._model = _UnifiedTagModel(tag_library_model)
        # The active tag-list filter: 'all' shows every tag, 'common' shows only
        # tags on every selected image (k == N), 'differences' shows only tags
        # missing from at least one (k < N).
        self._filter_mode = 'all'
        # Row to reselect after the next model refresh, so deleting/editing a
        # tag keeps the cursor at the same position instead of resetting to the
        # top (mirrors the normal Image Tags list). One-shot: set right before
        # an edit is requested, consumed by the next refresh.
        self._pending_anchor: int | None = None
        # Stable display order for the tag list. Every tag seen during the
        # current selection is assigned a permanent slot number the first time
        # it appears; the list is always shown sorted by that slot. Because a
        # slot is NEVER reused or pruned while the selection is unchanged, a tag
        # that is removed from every image (and so vanishes) reclaims its exact
        # original position if it comes back via undo/redo — independent of the
        # order it was removed from / re-added to the individual images. A
        # brand-new tag (manual add or auto-caption), or a fully-removed tag
        # that is manually re-added, gets a fresh slot at the end.
        # `_order_key` identifies the current selection (by image paths); when
        # it changes the slots are rebuilt from natural first-appearance order.
        self._order_key: frozenset[str] | None = None
        self._slot_of: dict[str, int] = {}
        self._next_slot = 0
        # Tags that were present at the previous refresh. Used to tell a tag
        # that (re)appeared on a normal edit (-> fresh slot at the end) from one
        # that was merely restored by undo/redo (-> keep its permanent slot).
        self._present_prev: set[str] = set()
        # Tags touched by the most recent auto-caption run (newly added or whose
        # image count increased). Shown with a subtle marker so they are easy to
        # review; cleared when the selection changes or a marked tag is clicked.
        self._recently_captioned: set[str] = set()
        # Per-tag image counts snapshotted when an auto-caption run starts, so
        # the touched set can be computed when it finishes. None outside a run.
        self._auto_caption_counts: dict[str, int] | None = None
        # Tags to select after the next refresh (undo/redo re-adds): mirrors the
        # single-image list, which selects a re-added tag. One-shot.
        self._pending_select_tags: list[str] = []
        # A re-added tag to scroll into view (without selecting) after the next
        # refresh, used on undo/redo when the "Do not auto-select newly added
        # tags" setting is on so the change stays visible. One-shot.
        self._pending_scroll_tag: str | None = None
        # The full ordered rows for the current selection (all tags, before the
        # "Only differences" filter) and the shared image count. Kept so the
        # filter can be toggled without recomputing from the images.
        self._rows_full: list[tuple[str, int]] = []
        self._total = 0

        # The clickable common/differences summary. Created here but placed by
        # ImageTagsEditor in the bottom status row (where the token count shows
        # for a single image). Clicking a half filters the tag list; the tag
        # list itself gets the full panel height. (The old top "Only
        # differences" button is gone; the editor now puts an Add Tag scope
        # button in that top slot instead.)
        self.summary_label = SummaryFilter()
        self.summary_label.half_clicked.connect(self._on_summary_half_clicked)

        self.tag_list = _TagListView()
        self.tag_list.setModel(self._model)
        self.tag_list.setItemDelegate(_TagBadgeDelegate(self))
        self.tag_list.delete_requested.connect(self._remove_selected)
        self.tag_list.customContextMenuRequested.connect(
            self._show_context_menu)
        self.tag_list.selectionModel().selectionChanged.connect(
            self._on_selection_changed)
        self.tag_list.edge_navigation.connect(self._on_edge)
        self.tag_list.escape_navigation.connect(
            self.escape_selection_requested)
        self._model.rename_requested.connect(self._on_rename_requested)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tag_list)

        self._refresh_summary()

    # ------------------------------------------------------------------
    # Population and stable ordering
    # ------------------------------------------------------------------
    def set_images(self, images: list[Image], restore_positions: bool = False):
        rows, total = compute_tag_rows(images)
        counts = dict(rows)
        present = [tag for tag, _ in rows]  # natural first-appearance order
        present_set = set(present)
        # When the selected image set changes, start a fresh stable order (and
        # drop any leftover auto-caption markers, which belonged to the old
        # selection).
        order_key = frozenset(str(image.path) for image in images)
        same_selection = order_key == self._order_key
        if not same_selection:
            self._order_key = order_key
            self._slot_of = {}
            self._next_slot = 0
            self._present_prev = set()
            self._recently_captioned = set()
        # When an external edit (auto-captioning, undo/redo) changes the tags of
        # the same selection without the panel setting an explicit row anchor,
        # remember the selected tags by name so they stay selected across the
        # model reset below, mirroring the normal Image Tags list. Panel edits
        # (delete/rename/add) instead restore the cursor by row via the pending
        # anchor, so skip name-based preservation for those.
        preserve = same_selection and self._pending_anchor is None
        prev_tags = self.tag_list.selected_tags() if preserve else []
        # Assign / keep permanent slots (see _slot_of docs in __init__).
        if not same_selection:
            # Fresh selection: slot every tag in natural first-appearance order.
            for tag in present:
                self._assign_slot(tag)
        elif restore_positions:
            # Undo/redo: never reassign an existing slot, so a returning tag
            # reclaims its exact position. Only a tag never seen this selection
            # (shouldn't normally happen on a restore) gets a new end slot.
            for tag in present:
                if tag not in self._slot_of:
                    self._assign_slot(tag)
        else:
            # Normal edit: a tag that just (re)appeared gets a fresh slot at the
            # end; tags already present keep their slots.
            for tag in present:
                if tag not in self._present_prev or tag not in self._slot_of:
                    self._assign_slot(tag, force_end=True)
        ordered_tags = sorted(present, key=lambda tag: self._slot_of[tag])
        self._rows_full = [(tag, counts[tag]) for tag in ordered_tags]
        self._total = total
        # Undo/redo that re-adds tag(s): honor the "Do not auto-select newly
        # added tags" setting, matching the single-image list. When auto-select
        # is on, pick the last re-added tag; when off, keep the user's current
        # selection but scroll the re-added tag into view so the undo/redo's
        # effect stays visible. `restore_positions` is set only for an undo/redo
        # history restore.
        if restore_positions and same_selection:
            readded = [tag for tag in ordered_tags
                       if tag not in self._present_prev]
            if readded:
                if not self._new_tag_auto_select_disabled():
                    self._pending_select_tags = readded[-1:]
                    prev_tags = []
                else:
                    self._pending_scroll_tag = readded[-1]
        self._present_prev = present_set
        self._refresh_view(prev_tags)

    @staticmethod
    def _new_tag_auto_select_disabled() -> bool:
        """Whether the "Do not auto-select newly added tags" setting is on."""
        return get_settings().value(
            'disable_new_tag_auto_select',
            defaultValue=DEFAULT_SETTINGS['disable_new_tag_auto_select'],
            type=bool)

    def _assign_slot(self, tag: str, force_end: bool = False):
        """Give ``tag`` a permanent slot. ``force_end`` (a manual/auto re-add of
        a tag that was gone) moves it to a fresh slot at the end even if it had
        one before."""
        if force_end or tag not in self._slot_of:
            self._slot_of[tag] = self._next_slot
            self._next_slot += 1

    def _refresh_view(self, prev_tags: list[str]):
        """Apply the current filter, repopulate the model, and restore state."""
        if self._filter_mode == 'differences':
            displayed = [(tag, count) for tag, count in self._rows_full
                         if count < self._total]
        elif self._filter_mode == 'common':
            displayed = [(tag, count) for tag, count in self._rows_full
                         if count >= self._total]
        else:
            displayed = list(self._rows_full)
        self._model.set_rows(displayed, self._total)
        self._refresh_summary()
        displayed_tags = [tag for tag, _ in displayed]
        # A pending re-added-tag selection (undo/redo) takes precedence over
        # restoring the cursor row or the prior selection.
        if self._pending_select_tags:
            wanted = self._pending_select_tags
            self._pending_select_tags = []
            self._pending_anchor = None
            self._reselect_tags(self.tag_list, displayed_tags, wanted)
            self.partial_focus_changed.emit(self._selected_difference_tags())
            return
        # Resetting the model above clears any selection. If an edit was just
        # made, restore the cursor to the same row so the position is preserved
        # (consistent with the normal Image Tags list).
        self._restore_anchor(self.tag_list, self._model, self._pending_anchor)
        self._pending_anchor = None
        # For external edits (or a filter toggle), reselect the previously
        # selected tags by name so the user's selection is maintained.
        if prev_tags:
            self._reselect_tags(self.tag_list, displayed_tags, prev_tags)
        # Honor "Do not auto-select newly added tags" on undo/redo: don't move
        # the selection, but scroll the re-added tag into view so the change is
        # still visible.
        if self._pending_scroll_tag is not None:
            tag = self._pending_scroll_tag
            self._pending_scroll_tag = None
            if tag in displayed_tags:
                self.tag_list.scrollTo(
                    self._model.index(displayed_tags.index(tag)))
        # Report the (possibly restored) difference selection so the grid split
        # stays in sync.
        self.partial_focus_changed.emit(self._selected_difference_tags())

    def _difference_tag_set(self) -> set[str]:
        return {tag for tag, count in self._rows_full if count < self._total}

    def _selected_difference_tags(self) -> list[str]:
        """The selected tags that are not on every image (drive the grid split).

        Selecting a common tag alone yields an empty list, so the grid reverts
        to its normal (unsplit) layout.
        """
        differences = self._difference_tag_set()
        return [tag for tag in self.tag_list.selected_tags()
                if tag in differences]

    @staticmethod
    def _anchor_row(list_view: '_TagListView') -> int | None:
        """The topmost selected row in a list, used as the restore position."""
        rows = [index.row() for index in list_view.selectedIndexes()]
        return min(rows) if rows else None

    def remember_anchor_for_add(self, list_view: '_TagListView'):
        """Stash the tag list's selected row so it is restored on the next
        refresh.

        Used when typing on the tag list auto-focuses the Add Tag box: after the
        tag is added (which resets the model and clears the selection), the
        previously selected tag is reselected, matching the normal Image Tags
        list.
        """
        self._pending_anchor = self._anchor_row(self.tag_list)

    @staticmethod
    def _restore_anchor(list_view: '_TagListView',
                        model: QAbstractListModel, anchor: int | None):
        """Reselect the tag now at ``anchor`` after a refresh.

        Matches the normal Image Tags list: select the row that took the deleted
        row's place, or the last row if the list got shorter.
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
        (auto-captioning, undo/redo) or a filter toggle that resets the model:
        any of ``wanted_tags`` still present is reselected at its new row.
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
        # Set the current (cursor) row WITHOUT disturbing the selection just
        # built above. ``setCurrentIndex`` issues a clear-and-select command in
        # the real windowed app, which wipes the multi-row selection;
        # ``NoUpdate`` moves only the cursor.
        selection_model.setCurrentIndex(
            model.index(rows[0]), QItemSelectionModel.SelectionFlag.NoUpdate)

    # ------------------------------------------------------------------
    # Selection / navigation
    # ------------------------------------------------------------------
    @Slot()
    def _on_selection_changed(self, *args):
        # Once the user acts on a captioned-tag marker (selects/clicks it), it
        # has served its "review me" purpose, so drop the marker for any tag now
        # selected and repaint just those rows.
        if self._recently_captioned:
            selected = set(self.tag_list.selected_tags())
            newly_cleared = self._recently_captioned & selected
            if newly_cleared:
                self._recently_captioned -= newly_cleared
                self.tag_list.viewport().update()
        self.partial_focus_changed.emit(self._selected_difference_tags())

    def begin_auto_caption_run(self):
        """Snapshot the current per-tag image counts so the next
        end_auto_caption_run() can flag which tags the run added or grew.

        Called when the Auto-Captioner starts while the grouped view is active.
        """
        self._auto_caption_counts = {tag: count
                                     for tag, count in self._rows_full}

    def end_auto_caption_run(self):
        """Mark every tag the just-finished auto-caption run added or increased
        the image count of, so the user can review them at a glance."""
        if self._auto_caption_counts is None:
            return
        before = self._auto_caption_counts
        self._auto_caption_counts = None
        touched = {tag for tag, count in self._rows_full
                   if count > before.get(tag, 0)}
        if touched != self._recently_captioned:
            self._recently_captioned = touched
            self.tag_list.viewport().update()

    @Slot(str)
    def _on_summary_half_clicked(self, half: str):
        # Clicking a half activates that filter; clicking the active half again
        # returns to 'all'. The filter drives the tag list; the editor listens
        # to filter_mode_changed to auto-set the Add Tag scope button.
        new_mode = 'all' if self._filter_mode == half else half
        if new_mode == self._filter_mode:
            return
        self._filter_mode = new_mode
        self._refresh_view(self.tag_list.selected_tags())
        self.filter_mode_changed.emit(new_mode)

    def reset_filter(self):
        """Return the tag-list filter to 'all' without emitting a change (used
        when entering the grouped view for a fresh selection)."""
        if self._filter_mode == 'all':
            return
        self._filter_mode = 'all'
        self._refresh_view(self.tag_list.selected_tags())

    def focus_first_tag(self) -> bool:
        """Focus the tag list and select its first tag.

        Used by the "Focus Image Tags List" shortcut while the grouped view is
        showing. Returns False when there is no tag to focus.
        """
        return self._focus_list_edge(self.tag_list, self._model, at_top=True)

    def _focus_list_edge(self, list_view: '_TagListView',
                         model: QAbstractListModel, at_top: bool) -> bool:
        """Move keyboard focus/selection to the top or bottom of the tag list.

        Returns False (so the caller can fall back to cycling images) when the
        list is hidden or empty.
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
    def _on_edge(self, direction: int):
        # Arrowing past the top/bottom of the single list moves to the previous
        # or next selected image, keeping the multi-image selection intact.
        self.cycle_image_requested.emit(direction)

    def _refresh_summary(self):
        total_tags = len(self._rows_full)
        difference_count = sum(1 for _, count in self._rows_full
                               if count < self._total)
        common_count = total_tags - difference_count
        self.summary_label.set_counts(common_count, difference_count)
        self.summary_label.set_active(self._filter_mode)

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------
    @Slot()
    def _remove_selected(self):
        tags = self.tag_list.selected_tags()
        if tags:
            self._pending_anchor = self._anchor_row(self.tag_list)
            self.remove_from_all_requested.emit(tags)
            self._pending_anchor = None

    @staticmethod
    def _tag_at(list_view: '_TagListView', position) -> str:
        """The tag under the given viewport position, or '' if none."""
        index = list_view.indexAt(position)
        if not index.isValid():
            return ''
        data = index.data(Qt.ItemDataRole.DisplayRole)
        return str(data) if data else ''

    def currently_selected_tags(self) -> list[str]:
        """Tags selected in the panel, for the grid cell context menu.

        Returns an empty list when nothing is selected.
        """
        return self.tag_list.selected_tags()

    def remember_selected_tags_anchor(self):
        """Stash the tag list's cursor row so it survives the next refresh.

        Used before a grid cell context-menu add/remove so the Image Tags pane
        keeps its position after the edit's model reset.
        """
        if self.tag_list.selected_tags():
            self.remember_anchor_for_add(self.tag_list)

    def selected_tag_for_wiki(self) -> str:
        """The single tag the wiki shortcut should look up from this panel.

        Mirrors ImageTagsList.selected_tag_for_wiki: returns a tag only when
        exactly one is selected, otherwise ''.
        """
        tags = self.tag_list.selected_tags()
        return tags[0] if len(tags) == 1 else ''

    def _copy_tags(self):
        self.tag_list.copy_selected_tags_to_clipboard()

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
        """Repaint the list so category color changes take effect."""
        self.tag_list.viewport().update()

    def _on_rename_requested(self, old_tag: str, new_tag: str):
        """Defer an in-place rename until the item editor has fully closed.

        Performing the rename synchronously inside the model's setData would
        reset the model while its editor is still closing, which can crash. So
        we stash the cursor position and fire the rename on the next event-loop
        tick, when the editor is gone.
        """
        anchor = self._anchor_row(self.tag_list)
        QTimer.singleShot(
            0, lambda: self._emit_rename(anchor, old_tag, new_tag))

    def _emit_rename(self, anchor: int | None, old_tag: str, new_tag: str):
        # Keep the renamed tag in its slot. A new name would otherwise be
        # treated as a brand-new tag and sent to the end, so move the old name's
        # permanent slot to the new name in place. The pending anchor then
        # restores the cursor to that row.
        self._rename_slot(old_tag, new_tag)
        self._pending_anchor = anchor
        self.rename_tag_requested.emit(old_tag, new_tag)
        self._pending_anchor = None

    def _rename_slot(self, old_tag: str, new_tag: str):
        """Move ``old_tag``'s permanent slot to ``new_tag`` so a rename keeps the
        tag's position. If ``new_tag`` already has a slot (the rename merges into
        an existing tag), keep that slot and just drop ``old_tag``. Also updates
        the "present at last refresh" set so the follow-up refresh doesn't treat
        ``new_tag`` as a freshly (re)appeared tag and bump it to the end."""
        if old_tag in self._slot_of:
            slot = self._slot_of.pop(old_tag)
            if new_tag not in self._slot_of:
                self._slot_of[new_tag] = slot
        if old_tag in self._present_prev:
            self._present_prev.discard(old_tag)
            self._present_prev.add(new_tag)
        if old_tag in self._recently_captioned:
            self._recently_captioned.discard(old_tag)
            self._recently_captioned.add(new_tag)

    def _show_context_menu(self, position):
        tags = self.tag_list.selected_tags()
        if not tags:
            return
        clicked_tag = self._tag_at(self.tag_list, position)
        differences = self._difference_tag_set()
        # "Add" only makes sense for tags missing from some images; offer it
        # when any selected tag is a difference (adding a common tag is a no-op).
        any_difference = any(tag in differences for tag in tags)
        menu = QMenu(self)
        copy_action = menu.addAction(
            'Copy Tags' if len(tags) > 1 else 'Copy Tag')
        menu.addSeparator()
        add_all_action = None
        add_current_action = None
        # Group by scope (all selected vs. current image) so choosing a scope is
        # a deliberate top-block vs. bottom-block decision, with a separator to
        # make the boundary unmistakable. Add sits before Remove within each.
        if any_difference:
            add_all_action = menu.addAction('Add to all selected')
        remove_all_action = menu.addAction('Remove from all selected')
        menu.addSeparator()
        if any_difference:
            add_current_action = menu.addAction('Add to current image')
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
        chosen = menu.exec(self.tag_list.viewport().mapToGlobal(position))
        if chosen is None:
            return
        # These actions don't reorder the list, so don't touch the anchor.
        if chosen == copy_action:
            self._copy_tags()
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
        self._pending_anchor = self._anchor_row(self.tag_list)
        if chosen == add_all_action:
            self.add_to_all_requested.emit(tags)
        elif chosen == add_current_action:
            self.add_to_current_requested.emit(tags)
        elif chosen == remove_all_action:
            self.remove_from_all_requested.emit(tags)
        elif chosen == remove_current_action:
            self.remove_from_current_requested.emit(tags)
        # Clear if the edit was a no-op (no refresh consumed the anchor).
        self._pending_anchor = None
