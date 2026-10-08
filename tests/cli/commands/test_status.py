from tightrein.cli import exit_codes


def test_no_command_prints_the_status_snapshot(cli):
    data = cli.json()
    assert (data["command"], data["exitCode"]) == ("status", exit_codes.OK)
    assert data["result"]["project"] == "shop"
    assert data["result"]["health"]["missed"] == 0  # 调度写的 schedule.missed，没有时为 0
    text = cli()
    assert text.code == exit_codes.OK and "shop" in text.out


def test_watch_json_takes_one_snapshot_and_writes_nothing(cli):
    data = cli.json("watch")
    assert data["exitCode"] == exit_codes.OK
    conn = cli.conn()
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    conn.close()
