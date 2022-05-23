import abc
import enum
import typing
import unicodedata

from . import utils
from .notes_db import NoteInfo


class SortMode(enum.Enum):
    # Sort in alphabetic order.
    ALPHA = 0
    # Sort by modification date.
    MODIFICATION_DATE = 1
    # Sort by creation date.
    CREATION_DATE = 2
    # Sort in alphanumeric order.
    ALPHA_NUM = 3


class Sorter(abc.ABC):
    """The abstract class to build extensible and flexible sorting logic.

    Usage:
        >>> sorter = MergedSorter(PinnedSorter(), AlphaSorter())
        >>> notes.sort(key=sorter)
    """

    @abc.abstractmethod
    def __call__(self, o: NoteInfo):
        raise NotImplementedError()


class NopSorter(Sorter):
    """Do nothing. The notes list retain original order. Use it to simplify complex sort logic."""

    def __call__(self, o: NoteInfo):
        return 0


class MergedSorter(Sorter):
    """Merge multiple sorters into a sorter. It realize sorting notes by multiple keys."""

    def __init__(self, *sorters: Sorter):
        self.sorters = sorters

    def __call__(self, o: NoteInfo):
        return tuple(s(o) for s in self.sorters)


class PinnedSorter(Sorter):
    """Sort that pinned notes are on top."""

    def __call__(self, o: NoteInfo):
        # Pinned notes on top.
        return 0 if utils.note_pinned(o.note) else 1


class AlphaSorter(Sorter):
    """Sort in alphabetically on note title."""

    def __call__(self, o: NoteInfo):
        return utils.get_note_title(o.note)


T = typing.TypeVar("T")


class AlphaNumSorter(Sorter):
    """Sort in alphanumeric order on note title."""

    class Nullable(typing.Generic[T]):
        """Null-safe comparable object for any types.

        Built-in types can not compare with None. For example, if you try to execute `1 < None`, it will raise a
        TypeError. The Nullable solves this problem, and further simplifies of comparison logic.
        """

        @classmethod
        def __class_getitem__(cls, item):
            return typing.TypeAlias()

        def __init__(self, val):
            self.val = val

        def __eq__(self, other):
            if not isinstance(other, AlphaNumSorter.Nullable):
                return NotImplemented
            return self.val == other.val

        def __gt__(self, other):
            if not isinstance(other, AlphaNumSorter.Nullable):
                return NotImplemented
            if self.val is None:
                return False
            else:
                if other.val is None:
                    return True
                return self.val > other.val

        def __repr__(self):
            return f"Nullable({repr(self.val)})"

    class Element(typing.NamedTuple):
        digits: "AlphaNumSorter.Nullable[int]"
        letters: "AlphaNumSorter.Nullable[str]"
        other: "AlphaNumSorter.Nullable[str]"

    def _enumerate_chars_with_category(self, s: str):
        for c in s:
            category = unicodedata.category(c)
            if category == "Nd":
                yield "numeric", c
            elif category[0] == "N" or category[0] == "L":
                yield "letter", c
            else:
                yield "other", c

    def _make_groups(self, iter_):
        # 連続した同じグループをグループ化
        s = ""
        last_category = ""
        for category, c in iter_:
            if last_category == category:
                s += c
            elif last_category != "":
                yield last_category, s
                last_category = category
                s = c
            elif last_category == "":
                last_category = category
                s = c
            else:
                raise RuntimeError("bug")
        yield last_category, s

    def _str2elements(self, s: str):
        if s == "":
            # The _make_groups() will yield an empty string ('') if s is ''. This behavior causes a crash on this
            # function. We should handle this case before executing _make_gropus().
            return AlphaNumSorter.Element(
                digits=AlphaNumSorter.Nullable(None),
                letters=AlphaNumSorter.Nullable(None),
                other=AlphaNumSorter.Nullable(None),
            )
        iter_ = self._enumerate_chars_with_category(s)
        groups = self._make_groups(iter_)
        for category, s in groups:
            digits = None
            letters = None
            others = None
            if category == "numeric":
                digits = int(s)
            elif category == "letter":
                letters = s
            elif category == "other":
                others = s
            else:
                raise RuntimeError("bug")
            yield AlphaNumSorter.Element(
                digits=AlphaNumSorter.Nullable(digits),
                letters=AlphaNumSorter.Nullable(letters),
                other=AlphaNumSorter.Nullable(others),
            )

    def __call__(self, o: NoteInfo):
        title = utils.get_note_title(o.note)
        return tuple(self._str2elements(title))


class DateSorter(Sorter):
    """Sort in creation/modification date."""

    def __init__(self, mode: SortMode):
        if mode == SortMode.MODIFICATION_DATE:
            self._sort_key = self._sort_key_modification_date
        elif mode == SortMode.CREATION_DATE:
            self._sort_key = self._sort_key_creation_date
        else:
            raise ValueError(f"invalid sort mode: {mode}")
        self.mode = mode

    def __call__(self, o: NoteInfo):
        return self._sort_key(o.note)

    def _sort_key_modification_date(self, note):
        # Last modified on top
        return -float(note.get("modifydate", 0))

    def _sort_key_creation_date(self, note):
        # Last modified on top
        return -float(note.get("createdate", 0))


def new_sorter(sort_mode, pinned_ontop):
    """Create new sorter based on sort_mode variable."""
    mode = SortMode(sort_mode)

    sorters = []
    if pinned_ontop:
        sorters.append(PinnedSorter())

    if mode == SortMode.ALPHA:
        sorters.append(AlphaSorter())
    elif mode in [SortMode.MODIFICATION_DATE, SortMode.CREATION_DATE]:
        sorters.append(DateSorter(mode=mode))
    elif mode == SortMode.ALPHA_NUM:
        sorters.append(AlphaNumSorter())
    else:
        raise ValueError(f"invalid sort_mode: {mode}")

    return MergedSorter(*sorters)
