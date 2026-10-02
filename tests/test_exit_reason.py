from sqlalchemy import text

from atr.appdb.repositories import _reason_code


def test_reason_code_is_the_rule_not_the_cut_sentence():
    assert _reason_code("stop_loss: down 10.2% against an average of 1,234.56") == "stop_loss"
    assert _reason_code("take_profit: up 4% against an average of 99") == "take_profit"
    assert _reason_code("time_stop") == "time_stop"


def test_old_truncated_rows_are_tidied_once(app_db):
    from datetime import datetime

    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(users.insert().values(
            user_id="u", email="t@example.com", username="t", display_name="T", password_hash="x",
            role="owner", is_active=True, mfa_enabled=False, failed_logins=0,
            created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        ))
    with app_db.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO trade_journal (trade_id, user_id, symbol, asset_class, side, quantity, entry_ts, entry_price, exit_reason, created_at) "
            "VALUES ('t1', 'u', 'X', 'EQUITY', 'BUY', 1, '2026-01-01', 10, 'stop_loss: down 10.2% against a', '2026-01-01')"
        ))
    app_db._normalise_exit_reasons()
    app_db._normalise_exit_reasons()  # idempotent
    with app_db.engine.begin() as conn:
        row = conn.execute(text("SELECT exit_reason, exit_detail FROM trade_journal WHERE trade_id='t1'")).one()
    assert row == ("stop_loss", "stop_loss: down 10.2% against a")
