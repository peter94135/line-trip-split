"""SQLite 連線、資料表初始化與欄位遷移。"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import URL, CheckConstraint, Engine, event, literal
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401，註冊資料表
from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite", database=str(settings.db_path)),
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def init_db() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _install_settlement_triggers(engine)


def _add_missing_columns(engine: Engine) -> None:
    """只自動新增可為 NULL 或有純量預設值的欄位。"""
    quote = engine.dialect.identifier_preparer.quote_identifier
    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            name = quote(table.name)
            existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({name})")}
            for column in table.columns:
                if column.name in existing:
                    continue
                sql_type = column.type.compile(engine.dialect)
                ddl = f"ALTER TABLE {name} ADD COLUMN {quote(column.name)} {sql_type}"
                if column.default is not None and column.default.is_scalar:
                    default = literal(column.default.arg).compile(
                        dialect=engine.dialect, compile_kwargs={"literal_binds": True},
                    )
                    ddl += f" DEFAULT {default}"
                elif not column.nullable:
                    raise RuntimeError(f"{table.name}.{column.name} 缺少預設值，請手動遷移")
                if not column.nullable:
                    ddl += " NOT NULL"
                for constraint in column.constraints:
                    if isinstance(constraint, CheckConstraint):
                        condition = constraint.sqltext.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
                        ddl += f" CHECK ({condition})"
                connection.exec_driver_sql(ddl)


def _install_settlement_triggers(engine: Engine) -> None:
    # 匯率補寫、修改後復原等變動也必須讓舊結算卡片失效。
    with engine.begin() as connection:
        for table in ("expense", "expenseshare", "transfer", "tripmember"):
            for operation, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                trip_id = (f"(SELECT trip_id FROM expense WHERE id = {row}.expense_id)"
                           if table == "expenseshare" else f"{row}.trip_id")
                connection.exec_driver_sql(f"""
                    CREATE TRIGGER IF NOT EXISTS settlement_{table}_{operation.lower()}
                    AFTER {operation} ON {table}
                    BEGIN
                        UPDATE trip SET ledger_version = ledger_version + 1 WHERE id = {trip_id};
                    END
                """)
        connection.exec_driver_sql("""
            CREATE TRIGGER IF NOT EXISTS settlement_trip_update
            AFTER UPDATE OF status, deleted_at, settle_currency ON trip
            WHEN OLD.status IS NOT NEW.status OR OLD.deleted_at IS NOT NEW.deleted_at
                OR OLD.settle_currency IS NOT NEW.settle_currency
            BEGIN
                UPDATE trip SET ledger_version = ledger_version + 1 WHERE id = NEW.id;
            END
        """)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
