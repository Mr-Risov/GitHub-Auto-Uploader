"""Professional keyboard-driven menus with Q and R shortcuts."""

from enum import Enum

import inquirer
from blessed import Terminal
from inquirer import errors, themes
from inquirer.render.console import ConsoleRender
from inquirer.render.console._list import List as InquirerListRenderer
from inquirer.render.console.base import MAX_OPTIONS_DISPLAYED_AT_ONCE
from readchar import key


class MenuAction(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE = "delete"
    SWITCH_ACCOUNT = "switch_account"
    RESET_AUTH = "reset_auth"
    CHECK_UPDATES = "check_updates"
    EXIT = "exit"
    REFRESH = "refresh"


class RepositoryAction(str, Enum):
    CREATE = "__create_repository__"
    BACK = "__back_from_repositories__"
    NO_MATCH = "__no_matching_repository__"


class ShortcutListRenderer(InquirerListRenderer):
    """Add single-key shortcuts without changing arrow/Enter behavior."""

    def process_input(self, pressed):
        shortcut = pressed.lower() if isinstance(pressed, str) else pressed
        shortcuts = getattr(self.question, "shortcuts", {})
        if shortcut in shortcuts:
            raise errors.EndOfInput(shortcuts[shortcut])
        return super().process_input(pressed)


class ShortcutConsoleRender(ConsoleRender):
    def render_factory(self, question_type):
        if question_type == "list":
            return ShortcutListRenderer
        return super().render_factory(question_type)


class SearchableRepositoryListRenderer(InquirerListRenderer):
    """Filter repository choices immediately as printable keys are pressed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.query = ""
        self.all_repositories = self.question.repository_names
        self.allow_create = self.question.allow_create
        self.base_message = self.question.repository_message
        self.visible_count = len(self.all_repositories)
        self._refresh_choices()
        self.option_area_height = min(
            MAX_OPTIONS_DISPLAYED_AT_ONCE,
            len(self.question.choices),
        )

    def _refresh_choices(self):
        folded_query = self.query.casefold()
        visible = [
            name
            for name in self.all_repositories
            if not folded_query or folded_query in name.casefold()
        ]
        self.visible_count = len(visible)
        choices = [(name, name) for name in visible]
        if self.query and not visible:
            choices.append((f"No matches for '{self.query}'", RepositoryAction.NO_MATCH))
        if self.allow_create:
            choices.append(("Create New Repository", RepositoryAction.CREATE))
        choices.append(("Back", RepositoryAction.BACK))
        self.question._choices = choices
        self.current = 0

    def get_header(self):
        search = self.query or "type to filter"
        return (
            f"{self.base_message} ({self.visible_count}/{len(self.all_repositories)})"
            f" • Search: {search}"
        )

    def get_hint(self):
        return "Type to filter • Backspace edit • Esc clear/back • Ctrl+Q back"

    def get_options(self):
        """Clear rows left behind when live filtering makes the list shorter."""
        rendered_options = list(super().get_options())
        yield from rendered_options
        for _index in range(self.option_area_height - len(rendered_options)):
            yield "", " ", self.theme.List.unselected_color

    def process_input(self, pressed):
        if pressed == "\x11":  # Ctrl+Q
            raise errors.EndOfInput(RepositoryAction.BACK)
        if pressed == key.ESC:
            if self.query:
                self.query = ""
                self._refresh_choices()
                return
            raise errors.EndOfInput(RepositoryAction.BACK)
        if pressed in (key.BACKSPACE, key.DELETE):
            if self.query:
                self.query = self.query[:-1]
                self._refresh_choices()
            return
        if pressed == key.ENTER:
            value = self.question.choices[self.current]
            if getattr(value, "value", value) == RepositoryAction.NO_MATCH:
                return
        if isinstance(pressed, str) and len(pressed) == 1 and pressed.isprintable():
            self.query += pressed
            self._refresh_choices()
            return
        return super().process_input(pressed)


class RepositoryConsoleRender(ConsoleRender):
    def render_factory(self, question_type):
        if question_type == "list":
            return SearchableRepositoryListRenderer
        return super().render_factory(question_type)


def _cyber_theme():
    terminal = Terminal()
    theme = themes.Default()
    theme.Question.mark_color = terminal.cyan
    theme.Question.brackets_color = terminal.cyan
    theme.List.selection_color = terminal.bold_cyan
    theme.List.selection_cursor = "▶"
    theme.List.unselected_color = terminal.normal
    return theme


def select_main_action():
    """Select a main action using arrows/Enter, Q to quit, or R to refresh."""
    question = inquirer.List(
        "action",
        message="Choose an action  [Q quit • R refresh]",
        choices=[
            ("Upload Folder to GitHub", MenuAction.UPLOAD),
            ("Download Repository from GitHub", MenuAction.DOWNLOAD),
            ("Delete a Repository", MenuAction.DELETE),
            ("Switch GitHub Account", MenuAction.SWITCH_ACCOUNT),
            ("Check for Updates", MenuAction.CHECK_UPDATES),
            ("Reset Authentication", MenuAction.RESET_AUTH),
            ("Exit", MenuAction.EXIT),
        ],
        carousel=True,
    )
    question.shortcuts = {
        "q": MenuAction.EXIT,
        "r": MenuAction.REFRESH,
    }
    answers = inquirer.prompt(
        [question],
        render=ShortcutConsoleRender(theme=_cyber_theme()),
        raise_keyboard_interrupt=True,
    )
    return answers["action"] if answers else MenuAction.EXIT


def select_repository(repositories, *, message="Select repository", allow_create=False, pinned=None):
    """Select a repository with immediate case-insensitive type-to-filter.

    Repositories listed in ``pinned`` (e.g. recently used ones) are shown first,
    in the given order, followed by the rest sorted alphabetically.
    """
    available = {name.casefold(): name for name in repositories}
    top = []
    for name in pinned or []:
        match = available.pop(name.casefold(), None) if name else None
        if match:
            top.append(match)
    all_repositories = top + sorted(available.values(), key=str.casefold)
    question = inquirer.List(
        "repository",
        message=message,
        choices=[],
        carousel=True,
    )
    question.repository_names = all_repositories
    question.repository_message = message
    question.allow_create = allow_create
    question.hints = {}
    answers = inquirer.prompt(
        [question],
        render=RepositoryConsoleRender(theme=_cyber_theme()),
        raise_keyboard_interrupt=True,
    )
    selection = answers["repository"] if answers else RepositoryAction.BACK
    return None if selection == RepositoryAction.BACK else selection
