"""On/off switches for the Danbooru and Gelbooru wikis.

Both are off by default: they show third-party (often NSFW) content, so the
user turns each one on in Settings after confirming a warning."""
from PySide6.QtWidgets import QMessageBox

from utils.settings import DEFAULT_SETTINGS, get_settings

DANBOORU = 'danbooru'
GELBOORU = 'gelbooru'
WIKI_SITE_NAMES = {DANBOORU: 'Danbooru', GELBOORU: 'Gelbooru'}
WIKI_SITE_HOSTS = {DANBOORU: 'danbooru.donmai.us', GELBOORU: 'gelbooru.com'}


def wiki_enabled_setting_key(site: str) -> str:
    return f'{site}_wiki_enabled'


def is_wiki_enabled(site: str) -> bool:
    key = wiki_enabled_setting_key(site)
    return get_settings().value(key, defaultValue=DEFAULT_SETTINGS[key],
                                type=bool)


def set_wiki_enabled(site: str, enabled: bool):
    get_settings().setValue(wiki_enabled_setting_key(site), bool(enabled))


def confirm_enable_wiki(parent, site: str) -> bool:
    """Warn about third-party / NSFW content; True if the user confirms."""
    name = WIKI_SITE_NAMES[site]
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(f'Enable {name} Wiki')
    box.setText(f'Enable the {name} Wiki?')
    box.setInformativeText(
        f'The {name} Wiki shows content pulled directly from {name} '
        f'({WIKI_SITE_HOSTS[site]}), a third-party website that TagGUI Plus '
        f'does not control.\n\n{name} is an adult image board, so wiki pages, '
        f'example posts and images may contain NSFW (explicit) material.')
    enable_button = box.addButton(f'Enable {name} Wiki',
                                  QMessageBox.ButtonRole.AcceptRole)
    cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel_button)
    box.setEscapeButton(cancel_button)
    box.exec()
    return box.clickedButton() is enable_button
