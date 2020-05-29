import pandas

from .table import Table

import flaggers.flagger as flagger

class Flags(Table):

    def __init__(self, user=None, passwd=None, hostname=None, db_name=None, schema="hive", engine=None):
        super().__init__(user, passwd, hostname, db_name, schema, engine)
        self._table_name = "flags"
        self._index_col = None
        # flag_id will be explicitly set by flag's enum values rather than
        # auto increment. This prevents strange duplicate flags with different
        # id when changing the flag enums.
        self._expected_cols = [
            "flag_id",
            "description",
            "name"
        ]
        self._creation_sql = "".join(["""
            CREATE TABLE IF NOT EXISTS """, self._schema, ".", self._table_name, """
            (
                flag_id INTEGER PRIMARY KEY,
                description VARCHAR(200),
                name VARCHAR(30)
            );"""])


    def write_table(self, flags):
        # flags is a list of [flag_id, flag_name]
        df = pandas.DataFrame(flags, columns=self._expected_cols)
        return self._write_table(df, conflict_columns=["flag_id"])


    def create_table(self):
        # Flags are written into the database on creation.
        if not super().create_table():
            return False

        flags = []
        for flag in flagger.Flags:
            fd = flagger.flag_descriptions[flag]
            flags.append([flag.value, fd.desc, fd.name])

        self.write_table(flags)
        return True
