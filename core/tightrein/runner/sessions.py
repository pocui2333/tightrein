"""交互会话记录的读写(architecture/02 2.8，agent_sessions 表)。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime

from tightrein.domain.enums import AgentSessionStatus
from tightrein.runner.task import RunnerTask
from tightrein.store.repos import agent_sessions
from tightrein.store.repos.agent_sessions import AgentSession


def open_session(conn: sqlite3.Connection, task: RunnerTask, tool: str, started_at: datetime,
                 session_id: str | None) -> AgentSession:
    session = AgentSession(task.stage, task.role, task.subject_id, started_at, tool, str(task.workdir),
                           AgentSessionStatus.OPEN, session_id)
    agent_sessions.save(conn, session)
    return session


def attach(conn: sqlite3.Connection, session: AgentSession, session_id: str | None) -> AgentSession:
    """会话开始后才能识别出会话 ID 的工具，结束时补记。"""
    if session_id is None or session_id == session.session_id:
        return session
    updated = replace(session, session_id=session_id)
    agent_sessions.save(conn, updated)
    return updated


def close(conn: sqlite3.Connection, session: AgentSession, ended_at: datetime) -> AgentSession:
    closed = replace(session, status=AgentSessionStatus.CLOSED, ended_at=ended_at)
    agent_sessions.save(conn, closed)
    return closed


def latest(conn: sqlite3.Connection, task: RunnerTask) -> AgentSession | None:
    """该对象、该角色最近一次会话。"""
    return agent_sessions.latest(conn, task.stage, task.role, task.subject_id)
