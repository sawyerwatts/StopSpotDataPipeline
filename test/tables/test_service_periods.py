import pytest
from sqlalchemy import create_engine
from src.tables import Service_Periods

@pytest.fixture
def service_periods_fixture():
    return Service_Periods("sw23", "fake")

@pytest.fixture
def dummy_engine():
    user = "sw23"
    passwd = "fake"
    hostname = "localhost"
    db_name = "idk_something"
    engine_info = "".join(["postgresql://", user, ":", passwd, "@", hostname, "/", db_name])
    return create_engine(engine_info), user, passwd, hostname, db_name

def test_constructor_build_engine(dummy_engine):
    expected, user, passwd, hostname, db_name = dummy_engine
    instance = Service_Periods(user, passwd, hostname, db_name)
    assert instance._engine.url == expected.url

def test_constructor_given_engine(dummy_engine):
    engine = dummy_engine[0]
    engine_url = engine.url
    instance = Service_Periods(engine=engine_url)
    assert instance._engine.url == engine.url

def test_chunksize(service_periods_fixture):
    chunksize = service_periods_fixture._chunksize
    assert int(chunksize) and chunksize > 1

def test_index_col(service_periods_fixture):
    assert service_periods_fixture._index_col == "service_key"

def test_table_name(service_periods_fixture):
    assert service_periods_fixture._table_name == "service_periods"

def test_schema(service_periods_fixture):
    assert service_periods_fixture._schema == "hive"

def test_expected_cols(service_periods_fixture):
    expected_cols = set(["month", "year", "ternary"])
    assert service_periods_fixture._expected_cols == expected_cols

def test_creation_sql(service_periods_fixture):
    # This tabbing is not accidental.
    expected = "".join(["""
            CREATE TABLE IF NOT EXISTS """, service_periods_fixture._schema, ".", service_periods_fixture._table_name, """
            (
                service_key BIGSERIAL PRIMARY KEY,
                month SMALLINT NOT NULL CHECK ( (month <= 12) AND (month >= 1) ),
                year SMALLINT NOT NULL CHECK (year > 1700),
                ternary SMALLINT NOT NULL CHECK ( (ternary <= 3) AND (ternary >= 1) ),
                UNIQUE (month, year, ternary)
            );"""])
    assert expected == service_periods_fixture._creation_sql

def test_get_engine(service_periods_fixture):
    assert service_periods_fixture.get_engine().url == service_periods_fixture._engine.url

# TODO: mock out
#   get_full_table
#   create_schema
#   delete_schema
#   create_table
#   delete_table

# TODO: test prompt w/ hidden=True and False


# TODO: When done testing this class, copy and adjust these tests for the other
# classes as well. CTran_Data will need to have custom tests for create_table.
