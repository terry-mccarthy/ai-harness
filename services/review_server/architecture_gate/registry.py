"""Language → checker list registry.

Add a new language by creating a Checker subclass and adding an entry here.
"""

from .base import Checker
from .checkers.import_linter import ImportLinterChecker
from .checkers.xenon_checker import XenonChecker


REGISTRY: dict[str, list[Checker]] = {
    "python": [
        ImportLinterChecker(),
        XenonChecker(),
    ],
}
