import datetime
import os

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
)

db = SqliteDatabase(None)  # Deferred init — bound in create_app()


class BaseModel(Model):
    class Meta:
        database = db


class Site(BaseModel):
    domain = CharField(unique=True)
    url = CharField()
    path = CharField()
    created_at = DateTimeField(default=datetime.datetime.now)


class HijackSession(BaseModel):
    site = ForeignKeyField(Site, backref="sessions", on_delete="CASCADE")
    started_at = DateTimeField(default=datetime.datetime.now)
    ended_at = DateTimeField(null=True)
    is_active = BooleanField(default=True)
    server_pid = IntegerField(null=True)
    cleaned_up = BooleanField(default=False)


def init_db(db_path: str):
    """Initialize the database at the given path."""
    db.init(db_path)
    db.connect(reuse_if_open=True)
    db.create_tables([Site, HijackSession])


def get_or_create_site(domain: str, url: str, path: str) -> Site:
    """Get existing site or create a new one."""
    site, _ = Site.get_or_create(
        domain=domain,
        defaults={"url": url, "path": path},
    )
    return site


def record_session_start(domain: str, server_pid: int) -> HijackSession:
    """Record a hijack session starting."""
    site = Site.get_or_none(Site.domain == domain)
    if not site:
        return None
    return HijackSession.create(site=site, server_pid=server_pid)


def record_session_end(domain: str):
    """Mark any active sessions for this domain as ended."""
    site = Site.get_or_none(Site.domain == domain)
    if not site:
        return
    (
        HijackSession.update(
            is_active=False,
            ended_at=datetime.datetime.now(),
            cleaned_up=True,
        )
        .where(HijackSession.site == site, HijackSession.is_active == True)
        .execute()
    )


def get_active_sessions() -> list[dict]:
    """Get all sessions marked active in the DB (may include stale ones)."""
    sessions = (
        HijackSession.select(HijackSession, Site)
        .join(Site)
        .where(HijackSession.is_active == True)
    )
    return [
        {
            "id": s.id,
            "domain": s.site.domain,
            "started_at": s.started_at,
            "server_pid": s.server_pid,
        }
        for s in sessions
    ]


def get_uncleaned_sessions() -> list[dict]:
    """Get sessions that ended but weren't properly cleaned up."""
    sessions = (
        HijackSession.select(HijackSession, Site)
        .join(Site)
        .where(
            HijackSession.is_active == True,
            HijackSession.cleaned_up == False,
        )
    )
    return [
        {
            "id": s.id,
            "domain": s.site.domain,
            "started_at": s.started_at,
            "server_pid": s.server_pid,
        }
        for s in sessions
    ]


def mark_sessions_cleaned(domain: str = None):
    """Mark sessions as cleaned up. If domain is None, mark all."""
    query = HijackSession.update(
        is_active=False,
        cleaned_up=True,
        ended_at=datetime.datetime.now(),
    ).where(HijackSession.is_active == True)

    if domain:
        site = Site.get_or_none(Site.domain == domain)
        if site:
            query = query.where(HijackSession.site == site)

    query.execute()


def get_session_history(limit: int = 50) -> list[dict]:
    """Get recent session history."""
    sessions = (
        HijackSession.select(HijackSession, Site)
        .join(Site)
        .order_by(HijackSession.started_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": s.id,
            "domain": s.site.domain,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "is_active": s.is_active,
            "cleaned_up": s.cleaned_up,
        }
        for s in sessions
    ]


def delete_site_record(domain: str):
    """Delete a site and its sessions from the DB."""
    site = Site.get_or_none(Site.domain == domain)
    if site:
        site.delete_instance(recursive=True)
