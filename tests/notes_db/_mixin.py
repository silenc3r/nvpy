import os
import shutil
from pathlib import Path
from nvpy.notes_db import DBConfig, NotesDB


class DBMixin:
    """Mixin class for test cases of the NotesDB class."""

    BASE_DIR = "/tmp/.nvpyUnitTests"

    def setUp(self):
        if os.path.isdir(self.BASE_DIR):
            shutil.rmtree(self.BASE_DIR)

    def _mock_config(self, notes_as_txt=False, simplenote_sync=False):
        conf = DBConfig(
            db_path=self.BASE_DIR,
            simplenote_sync=simplenote_sync,
            sn_username="",
            sn_password="",
            search_tags=1,
            notes_as_txt=notes_as_txt,
            txt_path=self.BASE_DIR + "/notes",
            replace_filename_spaces=False,
            read_txt_extensions="txt,mkdn,md,mdown,markdown",
        )

        return conf

    def _db(self, notes_as_txt=False, simplenote_sync=False):
        return NotesDB(self._mock_config(notes_as_txt, simplenote_sync))

    def _json_files(self):
        path = Path(self._mock_config().db_path)
        yield from (f.name for f in path.iterdir())

    def _text_files(self):
        path = Path(self._mock_config().txt_path)
        yield from (f.name for f in path.iterdir())
