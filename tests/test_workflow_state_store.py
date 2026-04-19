import sqlite3

from xobop.workflow_state import load_workflow_state_by_key, save_workflow_state


def _get_db_factory(conn):
    class _Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, exc_type, exc, tb):
            return False

    return lambda: _Ctx()


def test_save_and_load_workflow_state_roundtrip():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE workflow_state (chat_id INTEGER NOT NULL, key TEXT NOT NULL, value_json TEXT NOT NULL, updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(chat_id,key))"
    )

    get_db = _get_db_factory(conn)
    save_workflow_state(get_db=get_db, chat_id=123, key="pending_action", value={"action": "send_email"})
    out = load_workflow_state_by_key(get_db=get_db, key="pending_action")
    assert out[123]["action"] == "send_email"

    save_workflow_state(get_db=get_db, chat_id=123, key="pending_action", value=None)
    out2 = load_workflow_state_by_key(get_db=get_db, key="pending_action")
    assert 123 not in out2
