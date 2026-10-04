"""The ORM must match the SQL migration column-for-column."""

from sqlalchemy import inspect

from climate.store.orm import Base


def test_orm_matches_database(db):
    insp = inspect(db.get_bind())
    for table in Base.metadata.sorted_tables:
        db_cols = {c["name"] for c in insp.get_columns(table.name)}
        orm_cols = {c.name for c in table.columns}
        assert orm_cols == db_cols, f"{table.name}: orm-only {orm_cols - db_cols}, db-only {db_cols - orm_cols}"


def test_seed_inventory(db):
    from sqlalchemy import text

    assert db.execute(text("select count(*) from units")).scalar() == 3
    assert db.execute(text("select count(*) from rooms")).scalar() == 11
    assert db.execute(text("select count(*) from sensors")).scalar() == 9
    unsensored = set(db.execute(text("select key from rooms where not has_sensor")).scalars())
    assert unsensored == {"twins_room", "olive_room", "foyer"}


def test_factories_history(db):
    from tests.factories import make_history

    out = make_history(db, days=3)
    assert out["runtime"] == 3 * 288 * 3
