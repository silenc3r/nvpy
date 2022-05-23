# nvPY: cross-platform note-taking app with simplenote syncing
# copyright 2012 by Charl P. Botha <cpbotha@vxlabs.com>
# new BSD license
import abc
import base64
import codecs
import copy
import enum
import glob
import json
import logging
import os
import re
import sys
import threading
import time
import typing
import unicodedata
from http.client import HTTPException
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Any, Dict, List, Optional, Tuple

import simplenote  # type:ignore

from . import events, utils
from .debug import wrap_buggy_function
from .p3port import unicode

FilterResult = Tuple[List["NoteInfo"], str, int]


class Note(dict):
    @property
    def need_save(self):
        """Check if the local note need to save."""
        savedate = float(self["savedate"])
        return float(self["modifydate"]) > savedate or float(self["syncdate"]) > savedate

    @property
    def need_sync_to_server(self):
        """Check if the local note need to synchronize to the server.

        Return True when it has not key or it has been modified since last sync.
        """
        return "key" not in self or float(self["modifydate"]) > float(self["syncdate"])

    def is_newer_than(self, other):
        try:
            return float(self["modifydate"]) > float(other["modifydate"])
        except KeyError:
            return self["version"] > other["version"]

    def is_identical(self, other) -> bool:
        this_note = dict(self)
        other_note = dict(other)

        this_note["savedate"] = 0
        this_note["syncdate"] = 0
        other_note["savedate"] = 0
        other_note["syncdate"] = 0

        # convert to hashable objects.
        for k, v in this_note.items():
            if isinstance(v, list):
                this_note[k] = tuple(v)
        for k, v in other_note.items():
            if isinstance(v, list):
                other_note[k] = tuple(v)

        # should be an empty set when they're identical.
        difference = set(this_note.items()) ^ set(other_note.items())
        return len(difference) == 0


# API key provided for nvPY.
# Please do not use for other software!
simplenote.simplenote.API_KEY = bytes(reversed(base64.b64decode("OTg0OTI4ZTg4YjY0NzMyOTZjYzQzY2IwMDI1OWFkMzg=")))


# workaround for https://github.com/cpbotha/nvpy/issues/191
class Simplenote(simplenote.Simplenote):
    token: Optional[str]

    def get_token(self) -> str:
        if self.token is None:
            self.token = self.authenticate(self.username, self.password)
            if self.token is None:
                raise HTTPException("failed to connect to the server")
        try:
            return str(self.token, "utf-8")  # type: ignore
        except TypeError:
            return self.token

    def get_note(self, *args, **kwargs):
        try:
            res, status = super().get_note(*args, **kwargs)
        except HTTPException as e:
            res, status = e, -1
        if status == 0:
            res = Note(res)
        return res, status

    def update_note(self, *args, **kwargs):
        try:
            res, status = super().update_note(*args, **kwargs)
        except HTTPException as e:
            res, status = e, -1
        if status == 0:
            res = Note(res)
        return res, status

    def get_note_list(self, *args, **kwargs):
        try:
            res, status = super().get_note_list(*args, **kwargs)
        except HTTPException as e:
            res, status = e, -1
        if status == 0:
            res = [Note(n) for n in res]  # type: ignore
        return res, status


ACTION_SAVE = 0
ACTION_SYNC_PARTIAL_TO_SERVER = 1
ACTION_SYNC_PARTIAL_FROM_SERVER = 2  # UNUSED.


class SyncError(RuntimeError):
    pass


class ReadError(RuntimeError):
    pass


class WriteError(RuntimeError):
    pass


class UpdateResult(typing.NamedTuple):
    # Note object
    note: typing.Any
    is_updated: bool
    # Usually, error_object is None.  When failed to update, it have an error object.
    error_object: typing.Optional[typing.Any]


class NoteStatus(typing.NamedTuple):
    saved: bool
    synced: bool
    modified: bool
    full_syncing: bool


class NoteInfo(typing.NamedTuple):
    key: str
    note: typing.Any
    tagfound: int


class _BackgroundTask(typing.NamedTuple):
    action: int
    key: str
    note: typing.Any


class _BackgroundTaskReslt(typing.NamedTuple):
    action: int
    key: str
    note: typing.Any
    error: int


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


class NotesDB(utils.SubjectMixin):
    """NotesDB will take care of the local notes database and syncing with SN."""

    def __init__(self, config):
        utils.SubjectMixin.__init__(self)

        self.config = config

        self.store = FileStore(self.config)

        self.notes: Dict[str, Note] = self.store.load_notes()
        self.notes_lock = threading.Lock()

        # save and sync queue
        self.q_save = Queue()
        self.q_save_res = Queue()

        thread_save = Thread(target=wrap_buggy_function(self.worker_save))
        thread_save.setDaemon(True)
        thread_save.start()

        self.full_syncing = False

        # initialise the simplenote instance we're going to use
        # this does not yet need network access
        if self.config.simplenote_sync:
            self.simplenote = Simplenote(config.sn_username, config.sn_password)

            # reading a variable or setting this variable is atomic
            # so sync thread will write to it, main thread will only
            # check it sometimes.
            self.waiting_for_simplenote = False

            self.syncing_lock = Lock()

            self.q_sync = Queue()
            self.q_sync_res = Queue()

            thread_sync = Thread(target=wrap_buggy_function(self.worker_sync))
            thread_sync.setDaemon(True)
            thread_sync.start()

    def create_note(self, title: str) -> str:
        # need to get a key unique to this database. not really important
        # what it is, as long as it's unique.
        new_key = utils.generate_random_key()
        while new_key in self.notes:
            new_key = utils.generate_random_key()

        timestamp = time.time()

        # note has no internal key yet.
        new_note = Note(
            {
                "content": title,
                "modifydate": timestamp,
                "createdate": timestamp,
                "savedate": 0,  # never been written to disc
                "syncdate": 0,  # never been synced with server
                "tags": [],
            }
        )

        self.notes[new_key] = new_note

        return new_key

    def delete_note(self, key: str):
        n = self.notes[key]
        n["deleted"] = 1
        n["modifydate"] = time.time()

    def filter_notes(self, search_string: Optional[str] = None) -> FilterResult:
        """Return list of notes filtered with search string.

        Based on the search mode that has been selected in self.config,
        this method will call the appropriate helper method to do the
        actual work of filtering the notes.

        @param search_string: String that will be used for searching.
         Different meaning depending on the search mode.
        @return: notes filtered with selected search mode and sorted according
        to configuration. Two more elements in tuple: a regular expression
        that can be used for highlighting strings in the text widget; the
        total number of notes in memory.
        """

        if self.config.search_mode == "regexp":
            filtered_notes, match_regexp, active_notes = self.filter_notes_regexp(search_string)
        else:
            filtered_notes, match_regexp, active_notes = self.filter_notes_gstyle(search_string)

        filtered_notes.sort(key=self.config.sorter)
        return filtered_notes, match_regexp, active_notes

    def _helper_gstyle_tagmatch(self, tag_pats: List[str], note) -> int:
        """Check if note matches all tag patterns.

        Return values:
            0: no match - at least 1 pattern doesn't match
            1: all patterns match
            2: match becaus no patterns
        """
        if not tag_pats:
            # match because no tag: patterns were specified
            return 2

        tags = note.get("tags")
        if not tags:
            # no match because note has no tags
            return 0

        # for each tag_pat we have to find a matching tag
        for tp in tag_pats:
            matches = [tag for tag in tags if tag.startswith(tp)]
            if not matches:
                return 0

        # all patterns match
        return 1

    def _helper_gstyle_mswordmatch(self, msword_pats: List[str], content: str) -> bool:
        """Check if all patterns match the content."""

        # no search patterns, so note goes through
        if not msword_pats:
            return True

        for p in msword_pats:
            if p not in content:
                return False

        # all patterns match
        return True

    def filter_notes_gstyle(self, search_string: Optional[str] = None) -> FilterResult:
        filtered_notes = []
        # total number of notes, excluding deleted
        active_notes = 0

        if not search_string:
            with self.notes_lock:
                for k in self.notes:
                    n = self.notes[k]
                    if not n.get("deleted"):
                        active_notes += 1
                        filtered_notes.append(NoteInfo(key=k, note=n, tagfound=0))

            return filtered_notes, "", active_notes

        # group0: ag - not used
        # group1: t(ag)?:([^\s]+)
        # group2: multiple words in quotes
        # group3: single words
        # example result for 't:tag1 t:tag2 word1 "word2 word3" tag:tag3' ==
        # [('', 'tag1', '', ''), ('', 'tag2', '', ''), ('', '', '', 'word1'), ('', '', 'word2 word3', ''), ('ag', 'tag3', '', '')]

        # TODO: we should match when only open double-quote is present
        groups = re.findall(r't(ag)?:([^\s]+)|"([^"]+)"|([^\s]+)', search_string)
        tms_pats: List[List[str]] = [[] for _ in range(3)]

        # we end up with [[tag_pats],[multi_word_pats],[single_word_pats]]
        for gi in groups:
            for mi in range(1, 4):
                if gi[mi]:
                    tms_pats[mi - 1].append(gi[mi])

        tag_patterns = tms_pats[0]
        word_patterns = tms_pats[1] + tms_pats[2]

        with self.notes_lock:
            for k, n in self.notes.items():
                if n.get("deleted"):
                    continue

                active_notes += 1

                tagmatch = self._helper_gstyle_tagmatch(tag_patterns, n)
                if tagmatch == 0:
                    continue

                c = n["content"]
                # case insensitive mode: WARNING - SLOW!
                if not self.config.case_sensitive and c:
                    c = c.lower()
                msword_pats = word_patterns if self.config.case_sensitive else [p.lower() for p in word_patterns]
                if self._helper_gstyle_mswordmatch(msword_pats, c):
                    # we have a note that can go through!

                    # tagmatch == 1 if a tag was specced and found
                    # tagmatch == 2 if no tag was specced (so all notes go through)
                    tagfound = 1 if tagmatch == 1 else 0
                    # we have to store our local key also
                    filtered_notes.append(NoteInfo(key=k, note=n, tagfound=tagfound))

        match_regexp = "|".join(re.escape(p) for p in word_patterns)
        return filtered_notes, match_regexp, active_notes

    def filter_notes_regexp(self, search_string: Optional[str] = None) -> FilterResult:
        """Return list of notes filtered with search_string,
        a regular expression, each a tuple with (local_key, note).
        """
        match_regexp = ""
        sspat = None
        if search_string:
            try:
                if self.config.case_sensitive == 0:
                    sspat = re.compile(search_string, re.MULTILINE | re.I)
                else:
                    sspat = re.compile(search_string, re.MULTILINE)
                match_regexp = search_string
            except re.error:
                sspat = None

        filtered_notes = []
        # total number of notes, excluding deleted ones
        active_notes = 0
        with self.notes_lock:
            for k, n in self.notes.items():
                if n.get("deleted"):
                    continue

                active_notes += 1

                c: str = n["content"]
                if self.config.search_tags == 1:
                    t = n.get("tags")
                    if sspat:
                        if t and any(filter(lambda ti: sspat.search(ti), t)):  # type:ignore
                            # we have to store our local key also
                            filtered_notes.append(NoteInfo(key=k, note=n, tagfound=1))
                        elif sspat.search(c):
                            # we have to store our local key also
                            filtered_notes.append(NoteInfo(key=k, note=n, tagfound=0))
                    else:
                        # we have to store our local key also
                        filtered_notes.append(NoteInfo(key=k, note=n, tagfound=0))
                else:
                    if not sspat or sspat.search(c):
                        # we have to store our local key also
                        filtered_notes.append(NoteInfo(key=k, note=n, tagfound=0))

        return filtered_notes, match_regexp, active_notes

    def get_note(self, key: str):
        return self.notes[key]

    def get_note_content(self, key: str) -> str:
        with self.notes_lock:
            return self.notes[key]["content"]

    def get_note_status(self, key: str) -> NoteStatus:
        saved, synced, modified = False, False, False
        if key is not None:
            n = self.notes[key]
            modifydate = float(n["modifydate"])
            savedate = float(n["savedate"])

            if savedate > modifydate:
                saved = True
            else:
                modified = True

            if float(n["syncdate"]) > modifydate:
                synced = True

        return NoteStatus(saved=saved, synced=synced, modified=modified, full_syncing=self.full_syncing)

    def get_save_queue_len(self):
        return self.q_save.qsize()

    def get_sync_queue_len(self):
        return self.q_sync.qsize()

    def is_worker_busy(self):
        # XXX: this is only used for tests
        return bool(
            self.q_sync.qsize() or self.syncing_lock.locked() or self.waiting_for_simplenote or self.q_save.qsize()
        )

    def helper_save_note(self, key: str, note: Note):
        """Save a single note to disc."""
        self.store.save(key, note)

        # record that we saved this to disc.
        note["savedate"] = time.time()

    def sync_note_unthreaded(self, k: str):
        """Sync a single note with the server.

        Update existing note in memory with the returned data.
        This is a sychronous (blocking) call.
        """

        note = self.notes[k]

        if note.need_sync_to_server:
            # update to server
            result = self.update_note_to_server(note)

            if result.error_object is None:
                # success!
                n = result.note

                # if content was unchanged, there'll be no content sent back!
                new_content = "content" in n

                now = time.time()
                # 1. store when we've synced
                n["syncdate"] = now

                # update our existing note in-place!
                note.update(n)

                # return the key
                return (k, new_content)

            else:
                return None

        else:
            # our note is synced up, but we check if server has something new for us
            gret = self.simplenote.get_note(note["key"])

            if gret[1] == 0:
                n = gret[0]

                if n.is_newer_than(note):  # type: ignore
                    n["syncdate"] = time.time()  # type: ignore
                    note.update(n)
                    return (k, True)

                else:
                    return (k, False)

            else:
                return None

    def save_threaded(self):
        with self.notes_lock:
            for k, n in self.notes.items():
                if n.need_save:
                    cn = copy.deepcopy(n)
                    # put it on my queue as a save
                    o = _BackgroundTask(action=ACTION_SAVE, key=k, note=cn)
                    self.q_save.put(o)

        # in this same call, we process stuff that might have been put on the result queue
        nsaved = 0
        something_in_queue = True
        while something_in_queue:
            try:
                o = self.q_save_res.get_nowait()

            except Empty:
                something_in_queue = False

            else:
                # o (.action, .key, .note) is something that was written to disk
                # we only record the savedate.
                self.notes[o.key]["savedate"] = o.note["savedate"]
                self.notify_observers("change:note-status", events.NoteStatusChangedEvent(what="savedate", key=o.key))
                self.notify_observers("saved:note", events.NoteSavedEvent(key=o.key))
                nsaved += 1

        return nsaved

    def sync_to_server_threaded(self, wait_for_idle=True):
        """Only sync notes that have been changed / created locally since previous sync.

        This function is called by the housekeeping handler, so once every
        few seconds.

        @param wait_for_idle: Usually, last modification date has to be more
        than a few seconds ago before a sync to server is attempted. If
        wait_for_idle is set to False, no waiting is applied. Used by exit
        cleanup in controller.

        """
        # this many seconds of idle time (i.e. modification this long ago)
        # before we try to sync.
        if wait_for_idle:
            lastmod = 3
        else:
            lastmod = 0

        if not self.syncing_lock.acquire(blocking=False):
            # Currently, syncing_lock is locked by other thread.
            return 0, 0

        try:
            with self.notes_lock:
                now = time.time()
                for k, n in self.notes.items():
                    # if note has been modified since the sync, we need to sync.
                    # only do so if note hasn't been touched for 3 seconds
                    # and if this note isn't still in the queue to be processed by the
                    # worker (this last one very important)
                    modifydate = float(n.get("modifydate", -1))
                    syncdate = float(n.get("syncdate", -1))
                    need_sync = modifydate > syncdate and now - modifydate > lastmod
                    if need_sync:
                        task = _BackgroundTask(action=ACTION_SYNC_PARTIAL_TO_SERVER, key=k, note=None)
                        self.q_sync.put(task)

            # in this same call, we read out the result queue
            nsynced = 0
            nerrored = 0
            while True:
                try:
                    o: _BackgroundTaskReslt
                    o = self.q_sync_res.get_nowait()
                except Empty:
                    break

                okey = o.key
                if o.error:
                    nerrored += 1
                    continue

                # notify anyone (probably nvPY) that this note has been changed
                self.notify_observers("synced:note", events.NoteSyncedEvent(lkey=okey))

                nsynced += 1
                self.notify_observers("change:note-status", events.NoteStatusChangedEvent(what="syncdate", key=okey))

            return (nsynced, nerrored)
        finally:
            self.syncing_lock.release()

    def sync_full_threaded(self):
        thread_sync_full = Thread(target=self.sync_full_unthreaded)
        thread_sync_full.setDaemon(True)
        thread_sync_full.start()

    def sync_full_unthreaded(self):
        """Perform a full bi-directional sync with server.

        After this, it could be that local keys have been changed, so
        reset any views that you might have.
        """

        try:
            self.syncing_lock.acquire()

            self.full_syncing = True
            local_deletes = {}

            self.notify_observers("progress:sync_full", events.SyncProgressEvent(msg="Starting full sync."))
            # 1. Synchronize notes when it has locally changed.
            #    In this phase, synchronized all notes from client to server.
            with self.notes_lock:
                modified_notes = list(filter(lambda lk: Note(self.notes[lk]).need_sync_to_server, self.notes.keys()))
            for ni, lk in enumerate(modified_notes):
                with self.notes_lock:
                    n = self.notes[lk]
                if not Note(n).need_sync_to_server:
                    continue

                result = self.update_note_to_server(n)
                if result.error_object is None:
                    with self.notes_lock:
                        # replace n with result.note.
                        # if this was a new note, our local key is not valid anymore
                        del self.notes[lk]
                        # in either case (new or existing note), save note at assigned key
                        k = result.note.get("key")
                        # we merge the note we got back (content could be empty!)
                        n.update(result.note)
                        # and put it at the new key slot
                        self.notes[k] = n

                    # record that we just synced
                    n["syncdate"] = time.time()

                    # whatever the case may be, k is now updated
                    self.helper_save_note(k, n)
                    if lk != k:
                        # if lk was a different (purely local) key, should be deleted
                        local_deletes[lk] = True

                    self.notify_observers(
                        "progress:sync_full",
                        events.SyncProgressEvent(
                            msg="Synced modified note %d/%d to server." % (ni, len(modified_notes))
                        ),
                    )

                else:
                    key = n.get("key") or lk
                    msg = "Sync step 1 error - Could not update note {0} to server: {1}".format(
                        key, str(result.error_object)
                    )
                    logging.error(msg)
                    raise SyncError(msg)

            # 2. Retrieves full note list from server.
            #    In phase 2 to 5, synchronized all notes from server to client.
            self.notify_observers(
                "progress:sync_full",
                events.SyncProgressEvent(msg="Retrieving full note list from server, could take a while."),
            )
            self.waiting_for_simplenote = True
            nl = self.simplenote.get_note_list(data=False)
            self.waiting_for_simplenote = False
            if nl[1] == 0:
                nl = nl[0]
                self.notify_observers(
                    "progress:sync_full", events.SyncProgressEvent(msg="Retrieved full note list from server.")
                )

            else:
                error = nl[0]
                msg = "Could not get note list from server: %s" % str(error)
                logging.error(msg)
                raise SyncError(msg)

            # 3. Delete local notes not included in full note list.
            server_keys = {}
            for n in nl:  # type: ignore
                k = n.get("key")
                server_keys[k] = True

            with self.notes_lock:
                for lk in list(self.notes.keys()):
                    if lk not in server_keys:
                        if self.notes[lk]["syncdate"] == 0:
                            # This note MUST NOT delete because it was created during phase 1 or phase 2.
                            continue

                        if self.config.notes_as_txt:
                            tfn = os.path.join(
                                self.config.txt_path,
                                utils.get_note_title_file(self.notes[lk], self.config.replace_filename_spaces),
                            )
                            if os.path.isfile(tfn):
                                os.unlink(tfn)
                        del self.notes[lk]
                        local_deletes[lk] = True

            self.notify_observers(
                "progress:sync_full", events.SyncProgressEvent(msg="Deleted note %d." % (len(local_deletes)))
            )

            # 4. Update local notes.
            lennl = len(nl)
            sync_from_server_errors = 0
            for ni, n in enumerate(nl):
                k = n.get("key")
                if k in self.notes:
                    # n is already exists in local.
                    if Note(n).is_newer_than(self.notes[k]):
                        # We must update local note with remote note.
                        err = 0
                        if "content" not in n:
                            # The content field is missing.  Get all data from server.
                            self.waiting_for_simplenote = True
                            n, err = self.simplenote.get_note(k)
                            self.waiting_for_simplenote = False

                        if err == 0:
                            self.notes[k].update(n)
                            self.notes[k]["syncdate"] = time.time()
                            self.helper_save_note(k, self.notes[k])
                            self.notify_observers(
                                "progress:sync_full",
                                events.SyncProgressEvent(msg="Synced newer note %d (%d) from server." % (ni, lennl)),
                            )

                        else:
                            err_obj = n
                            logging.error("Error syncing newer note %s from server: %s" % (k, err_obj))
                            sync_from_server_errors += 1

                else:
                    # n is new note.
                    # We must save it in local.
                    err = 0
                    if "content" not in n:
                        # The content field is missing.  Get all data from server.
                        self.waiting_for_simplenote = True
                        n, err = self.simplenote.get_note(k)
                        self.waiting_for_simplenote = False

                    if err == 0:
                        with self.notes_lock:
                            self.notes[k] = n  # type: ignore
                            # never been written to disc
                            n["savedate"] = 0  # type: ignore
                            n["syncdate"] = time.time()  # type: ignore
                            self.helper_save_note(k, n)
                            self.notify_observers(
                                "progress:sync_full",
                                events.SyncProgressEvent(msg="Synced new note %d (%d) from server." % (ni, lennl)),
                            )

                    else:
                        err_obj = n
                        logging.error("Error syncing new note %s from server: %s" % (k, err_obj))
                        sync_from_server_errors += 1

            # 5. Clean up local notes.
            for dk in local_deletes.keys():
                self.store.delete(dk)

            self.notify_observers("complete:sync_full", events.SyncCompletedEvent(errors=sync_from_server_errors))

        except Exception as e:
            # Report an error to UI thread.
            self.notify_observers("error:sync_full", events.SyncFailedEvent(error=e, exc_info=sys.exc_info()))

        finally:
            self.full_syncing = False
            self.syncing_lock.release()

    def set_note_content(self, key: str, content: str):
        n = self.notes[key]
        old_content = n.get("content")
        if content != old_content:
            n["content"] = content
            n["modifydate"] = time.time()
            self.notify_observers("change:note-status", events.NoteStatusChangedEvent(what="modifydate", key=key))

    def delete_note_tag(self, key: str, tag: str):
        note = self.notes[key]
        note_tags = list(note["tags"])
        note_tags.remove(tag)
        note["tags"] = note_tags
        note["modifydate"] = time.time()
        self.notify_observers("change:note-status", events.NoteStatusChangedEvent(what="modifydate", key=key))

    def add_note_tags(self, key: str, comma_separated_tags: str):
        new_tags = utils.sanitise_tags(comma_separated_tags)
        note = self.notes[key]
        tags_set = set(note["tags"]) | set(new_tags)
        note["tags"] = sorted(tags_set)
        note["modifydate"] = time.time()
        self.notify_observers("change:note-status", events.NoteStatusChangedEvent(what="modifydate", key=key))

    def set_note_pinned(self, key: str, pinned: int):
        n = self.notes[key]
        old_pinned = utils.note_pinned(n)
        if pinned != old_pinned:
            if "systemtags" not in n:
                n["systemtags"] = []

            systemtags = n["systemtags"]

            if pinned:
                # which by definition means that it was NOT pinned
                systemtags.append("pinned")

            else:
                systemtags.remove("pinned")

            n["modifydate"] = time.time()
            self.notify_observers("change:note-status", events.NoteStatusChangedEvent(what="modifydate", key=key))

    def worker_save(self):
        while True:
            o = self.q_save.get()

            if o.action == ACTION_SAVE:
                # this will write the savedate into o.note
                # with filename o.key.json
                try:
                    self.helper_save_note(o.key, o.note)

                except WriteError as e:
                    logging.error("FATAL ERROR in access to file system")
                    print("FATAL ERROR: Check the nvpy.log")
                    os._exit(1)

                else:
                    # put the whole thing back into the result q
                    # now we don't have to copy, because this thread
                    # is never going to use o again.
                    # somebody has to read out the queue...
                    self.q_save_res.put(o)

    def worker_sync(self):
        while True:
            task: _BackgroundTask
            task = self.q_sync.get()
            with self.syncing_lock:
                if task.action == ACTION_SYNC_PARTIAL_TO_SERVER:
                    res = self._worker_sync_to_server(task.key)
                    self.q_sync_res.put(res)
                else:
                    raise RuntimeError(f"invalid action: {task.action}")

    def _worker_sync_to_server(self, key: str):
        """Sync a note to server. It is internal function of worker_sync().
        Caller MUST acquire the syncing_lock, and MUST NOT acquire the notes_lock.
        """
        action = ACTION_SYNC_PARTIAL_TO_SERVER
        with self.notes_lock:
            syncdate = time.time()
            note = self.notes[key]
            if not Note(note).need_sync_to_server:
                # The note already synced with server.
                return _BackgroundTaskReslt(action=action, key=key, note=None, error=0)
            local_note = copy.deepcopy(note)

        if "key" in local_note:
            logging.debug("Updating note %s (local key %s) to server." % (local_note["key"], key))
        else:
            logging.debug("Sending new note (local key %s) to server." % (key,))

        result = self.update_note_to_server(local_note)
        if result.error_object is not None:
            return _BackgroundTaskReslt(action=action, key=key, note=None, error=1)

        with self.notes_lock:
            note = self.notes[key]
            remote_note = result.note
            if float(local_note["modifydate"]) < float(note["modifydate"]):
                # The user has changed a local note during sync with server. Just record version that we got from
                # simplenote server. If we don't do this, merging problems start happening.
                #
                # VERY importantly: also store the key. It could be that we've just created the note, but that the user
                # continued typing. We need to store the new server key, else we'll keep on sending new notes.
                note["version"] = remote_note["version"]
                note["syncdate"] = syncdate
                note["key"] = remote_note["key"]
                return _BackgroundTaskReslt(action=action, key=key, note=None, error=0)

            if result.is_updated:
                if remote_note.get("content", None) is None:
                    # If note has not been changed, we don't get content back. To prevent overriding of content,
                    # we should remove the content from remote_note.
                    remote_note.pop("content", None)
                note.update(remote_note)
            note["syncdate"] = syncdate
            return _BackgroundTaskReslt(action=action, key=key, note=None, error=0)

    def update_note_to_server(self, note):
        """Update the note to simplenote server.

        :return: UpdateResult object
        """

        self.waiting_for_simplenote = True
        # WORKAROUND: simplenote <=v2.1.2 modifies the note passed by argument. To prevent on-memory database
        #             corruption, Copy the note object before it is passed to simplenote library.
        # https://github.com/cpbotha/nvpy/issues/181#issuecomment-489543782
        o, err = self.simplenote.update_note(note.copy())
        self.waiting_for_simplenote = False

        if err == 0:
            # success!

            # Keeps the internal fields of nvpy.
            new_note = dict(note)
            new_note.update(o)

            logging.debug("Server replies with updated note " + new_note["key"])
            return UpdateResult(
                note=new_note,
                is_updated=True,
                error_object=None,
            )

        update_error = o

        if "key" in note:
            # Note has already been saved on the simplenote server.
            # Try to recover the update error.
            self.waiting_for_simplenote = True
            o, err = self.simplenote.get_note(note["key"])
            self.waiting_for_simplenote = False

            if err == 0:
                local_note = note
                remote_note = o

                if local_note.is_identical(remote_note):
                    # got an error response when updating the note.
                    # however, the remote note has been updated.
                    # this phenomenon is rarely occurs.
                    # if it occurs, housekeeper's is going to repeatedly update this note.
                    # regard updating error as success for prevent this problem.
                    logging.info(
                        "Regard updating error (local key %s, error object %s) as success."
                        % (local_note["key"], repr(update_error))
                    )
                    return UpdateResult(
                        note=local_note,
                        is_updated=False,
                        error_object=None,
                    )

                else:
                    # Local note and remote note are different.  But failed to update.
                    logging.error(
                        "Could not update note %s to server: %s, local=%s, remote=%s"
                        % (note["key"], update_error, local_note, remote_note)
                    )
                    return UpdateResult(
                        note=None,
                        is_updated=False,
                        error_object=update_error,
                    )

            else:
                get_error = o
                logging.error(
                    "Could not get/update note %s: update_error=%s, get_error=%s"
                    % (note["key"], update_error, get_error)
                )
                return UpdateResult(
                    note=None,
                    is_updated=False,
                    error_object={"update_error": update_error, "get_error": get_error},
                )

        # Failed to create new note.
        assert err
        assert "key" not in note
        return UpdateResult(note=None, is_updated=False, error_object=update_error)


class FileStore:
    def __init__(self, config):
        self.config = config
        self.db_path = Path(self.config.db_path)
        if self.config.notes_as_txt:
            self.titlelist = {}

        # create db dir if it does not exist
        if not self.db_path.exists():
            self.db_path.mkdir()

    def load_notes(self):
        if self.config.notes_as_txt:
            return self.read_all_txt()
        else:
            return self.read_all_json()

    def read_all_json(self) -> Dict[str, Note]:
        now = time.time()
        notes = {}
        fnlist = self.db_path.glob("*.json")
        for fn in fnlist:
            localkey = fn.stem
            with open(fn, "rb") as f:
                n = Note(json.load(f))
            n["savedate"] = now
            notes[localkey] = n

        return notes

    def read_all_txt(self) -> Dict[str, Note]:
        """Load notes from txt files in really complicated way."""

        # I don't know if this function even works.

        # create txt Notes dir if it does not exist
        if not os.path.exists(self.config.txt_path):
            os.mkdir(self.config.txt_path)

        now = time.time()
        notes = {}
        fnlist = list(self.db_path.glob("*.json"))
        txtlist = []

        for ext in self.config.read_txt_extensions.split(","):
            txtlist += glob.glob(self.config.txt_path + "/*." + ext)

        # removing json files and force full full sync if using text files
        # and none exists and json files are there
        if self.config.notes_as_txt and not txtlist and fnlist:
            logging.debug("Forcing resync: using text notes, first usage")
            for fn in fnlist:
                os.unlink(fn)
            fnlist = []

        for fn in fnlist:
            try:
                with open(fn, "rb") as f:
                    n = Note(json.load(f))

                nt = utils.get_note_title_file(n, self.config.replace_filename_spaces)
                tfn = os.path.join(self.config.txt_path, nt)
                if os.path.isfile(tfn):
                    self.titlelist[n.get("key")] = nt
                    txtlist.remove(tfn)
                    if os.path.getmtime(tfn) > os.path.getmtime(fn):
                        logging.debug("Text note was changed: %s" % (fn,))
                        with codecs.open(tfn, mode="rb", encoding="utf-8") as f:
                            c = f.read()

                        n["content"] = c
                        n["modifydate"] = os.path.getmtime(tfn)
                else:
                    logging.debug("Deleting note : %s" % (fn,))
                    if not self.config.simplenote_sync:
                        os.unlink(fn)
                        continue
                    else:
                        n["deleted"] = 1
                        n["modifydate"] = now

            except IOError as e:
                logging.error("NotesDB_init: Error opening %s: %s" % (fn, str(e)))
                raise ReadError("Error opening note file")

            except ValueError as e:
                logging.error("NotesDB_init: Error reading %s: %s" % (fn, str(e)))
                raise ReadError("Error reading note file")

            else:
                # we always have a localkey, also when we don't have a note['key'] yet (no sync)
                localkey = os.path.splitext(os.path.basename(fn))[0]
                notes[localkey] = n
                # we maintain in memory a timestamp of the last save
                # these notes have just been read, so at this moment
                # they're in sync with the disc.
                n["savedate"] = now

        for fn in txtlist:
            logging.debug("New text note found : %s" % (fn,))
            tfn = os.path.join(self.config.txt_path, fn)
            try:
                with codecs.open(tfn, mode="rb", encoding="utf-8") as f:
                    c = f.read()

            except IOError as e:
                logging.error("NotesDB_init: Error opening %s: %s" % (fn, str(e)))
                raise ReadError("Error opening note file")

            except ValueError as e:
                logging.error("NotesDB_init: Error reading %s: %s" % (fn, str(e)))
                raise ReadError("Error reading note file")

            else:
                nk = utils.generate_random_key()
                while nk in notes:
                    nk = utils.generate_random_key()
                new_note = Note(
                    {
                        "content": c,
                        "modifydate": now,
                        "create": now,
                        "savedate": 0,
                        "syncdate": 0,
                        "tags": [],
                    }
                )
                notes[nk] = new_note
                nn = os.path.splitext(os.path.basename(fn))[0]
                if nn != utils.get_note_title(notes[nk]):
                    notes[nk]["content"] = nn + "\n\n" + c

                os.unlink(tfn)

        return notes

    def save(self, key: str, note: Note):
        if self.config.notes_as_txt:
            self.save_txt(key, note)
        self.save_json(key, note)

    def save_txt(self, key, note):
        t = utils.get_note_title_file(note, self.config.replace_filename_spaces)
        if t and not note.get("deleted"):
            if key in self.titlelist:
                logging.debug("Writing note : %s %s", t, self.titlelist[key])
                if self.titlelist[key] != t:
                    dfn = Path(self.config.txt_path) / self.titlelist[key]
                    if Path.is_file(dfn):
                        logging.debug("Delete file %s ", dfn)
                        dfn.unlink()
                    else:
                        logging.debug("File not exits %s ", dfn)
            else:
                logging.debug("Key not in list %s ", key)

            self.titlelist[key] = t
            fn = Path(self.config.txt_path) / t
            try:
                fn.write_text(note["content"], encoding="utf-8")
            except (IOError, ValueError) as e:
                logging.error("NotesDB_save: Error writing %s: %s", fn, str(e))
                raise WriteError(f"Error writing note file ({fn})")

        elif t and note.get("deleted") and key in self.titlelist:
            dfn = Path(self.config.txt_path) / self.titlelist[key]
            if Path.is_file(dfn):
                logging.debug("Delete file %s ", dfn)
                dfn.unlink()

    def save_json(self, key, note):
        """Write note to disk as json file. This is default note format."""
        filename = self._key_to_fname(key)

        if not self.config.simplenote_sync and note.get("deleted"):
            if Path.is_file(filename):
                filename.unlink()
            return

        try:
            filename.write_text(json.dumps(note, indent=2), encoding="utf-8")
        except (IOError, ValueError) as e:
            logging.error("NotesDB_save: Error opening %s: %s", filename, str(e))
            raise WriteError(f"Error writing note file ({filename})")

    def delete(self, key: str) -> None:
        fname = self._key_to_fname(key)
        if fname.exists():
            fname.unlink()

    def _key_to_fname(self, key: str) -> Path:
        return self.db_path / (key + ".json")
