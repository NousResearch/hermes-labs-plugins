"""#59: --decisions surfaces the question behind an input-blocked kanban task."""

from __future__ import annotations

import sqlite3

import pytest

from inbox_plugin import collectors as c
from inbox_plugin.report import build_inbox, render_inbox


@pytest.fixture
def board(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
            status TEXT, created_at REAL, started_at REAL, block_kind TEXT);
        CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            author TEXT, body TEXT, created_at REAL);
    """)
    conn.execute("INSERT INTO tasks VALUES ('t1','Pick a DB','body question','sal','blocked',1,1,'needs_input')")
    conn.execute("INSERT INTO tasks VALUES ('t2','Waiting on dep','dep body','sal','blocked',2,2,'dependency')")
    conn.execute("INSERT INTO tasks VALUES ('t3','Running','run body','sal','running',3,3,NULL)")
    conn.execute("INSERT INTO task_comments (task_id,author,body,created_at) VALUES ('t1','worker','Postgres or SQLite?',9)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(c, "kanban_db_location", lambda: db)
    return db


def test_only_needs_input_blocks_are_decisions(board):
    items, note = c.collect_decisions()
    assert note is None
    assert [i["id"] for i in items] == ["t1"]


def test_latest_comment_is_the_question(board):
    items, _ = c.collect_decisions()
    assert items[0]["question"] == "Postgres or SQLite?"
    assert items[0]["question_source"] == "comment"
    assert items[0]["asked_by"] == "worker"


def test_falls_back_to_body_without_comments(board):
    conn = sqlite3.connect(board)
    conn.execute("DELETE FROM task_comments")
    conn.commit()
    conn.close()
    items, _ = c.collect_decisions()
    assert items[0]["question"] == "body question"
    assert items[0]["question_source"] == "body"


def test_absent_board_is_noted(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "kanban_db_location", lambda: tmp_path / "nope.db")
    items, note = c.collect_decisions()
    assert items == [] and note == "absent"


def test_flag_is_opt_in(board, tmp_path):
    off = build_inbox(home=tmp_path)
    assert "decisions" not in off
    on = build_inbox(home=tmp_path, decisions=True)
    assert [d["id"] for d in on["decisions"]] == ["t1"]
    assert on["sources"]["decisions"] == "read"


def test_render_shows_the_question(board, tmp_path):
    text = render_inbox(build_inbox(home=tmp_path, decisions=True))
    assert "Decisions waiting on you (1)" in text
    assert "Postgres or SQLite?" in text


def test_render_unchanged_without_flag(board, tmp_path):
    assert "Decisions waiting on you" not in render_inbox(build_inbox(home=tmp_path))
