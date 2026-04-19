import sqlite3

from xobop.simulation.replay_controller import apply_artifact_to_temp_settings


def test_apply_skill_policy_artifact_to_temp_db(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, timestamp TEXT)")
        con.execute("CREATE TABLE skill_policies (skill_name TEXT, policy_key TEXT, policy_value TEXT, PRIMARY KEY(skill_name, policy_key))")
        con.commit()
    finally:
        con.close()

    art = {
        "artifact_type": "skill_policy_change",
        "target": "runtime_skill_config",
        "artifact_json": {"skill": "run_shell", "policy": "write_root", "value": "/tmp"},
    }
    apply_artifact_to_temp_settings(str(db), art)

    con = sqlite3.connect(str(db))
    try:
        row = con.execute("SELECT policy_value FROM skill_policies WHERE skill_name='run_shell' AND policy_key='write_root'").fetchone()
        assert row and row[0] == "/tmp"
    finally:
        con.close()
