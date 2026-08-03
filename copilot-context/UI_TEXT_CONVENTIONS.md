# UI Text & Capitalization Conventions

These rules keep user-facing text in TagGUI consistent. Follow them for any
new or changed label, button, menu item, dialog, tooltip, or message.

## Two-tier capitalization rule

**Title Case** — things you *do* or *open* (actionable or heading elements):

- Buttons (`QPushButton`)
- Menu items and context-menu actions (`QAction`, `menu.addAction(...)`)
- Tab titles, pane titles, dialog window titles, settings section headers

Examples: `Remove Selected Tags`, `View Danbooru Wiki`, `Sort Tags
Alphabetically`, `Add to Tag Library`, `Reset All to Defaults`, `Rename Tag...`,
`Tag Library Panel`.

**Sentence case** — things you *label* or *describe*:

- Field labels next to inputs (`QLabel` for a combo box, spin box, line edit)
- Checkbox labels
- Descriptive settings rows
- Confirmation / information / warning message body text

Examples: `Find text`, `Whole tags only`, `Show resolution badge`,
`Auto-apply implications when adding tags`, `Image editor executable`,
`Natural language mode`.

## Title Case word rules (AP/Chicago style)

Capitalize the first and last word and all major words (nouns, verbs,
adjectives, adverbs, pronouns). Lowercase minor words **unless** they are the
first or last word:

- Articles: a, an, the
- Coordinating conjunctions: and, or, but, nor, so, yet
- Short prepositions: of, to, in, for, on, at, by, up, as

Also:

- Capitalize the word immediately after a colon.
- Capitalize both parts of a hyphenated major term (e.g. `Load in 4-bit`).
- Keep the trailing `...` on any command that opens a further dialog or picker
  (e.g. `Manage Library...`, `Select Directory...`).

## Feature (proper) names

Recognized feature names are always capitalized in Title Case **wherever they
appear**, including inside sentence-case labels and message body text — the same
way a product name would be. Current feature names:

- **Tag Library**
- **Danbooru Wiki**
- **Gelbooru Wiki**
- **All Tags**, **Image Tags** (pane names)

Examples:

- Sentence-case label: `Ask before removing from Tag Library`
- Message body: `Replaced Tag Library with 42 tags.`
- Button: `Add to Tag Library`

Do **not** apply these capitalization rules to code comments, docstrings,
variable names, or internal setting keys — only to text shown to the user.
