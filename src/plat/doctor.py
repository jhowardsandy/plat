"""Read-only qualification: distinguish installed from verified reachability."""
import shutil
from pathlib import Path

from sqlalchemy import text

from . import db as DB
from .config import load


def check():
    cfg = load()
    checks = []
    def add(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
    add('workspace', cfg.workspace_root.is_dir(), str(cfg.workspace_root))
    add('git', shutil.which('git'), 'required for isolated worktrees')
    for name in ('claude', 'codex'):
        add(name, shutil.which(name), 'installed only; run plat probe to verify account access and contracts')
    try:
        roles = cfg.roles()
        from .roles import bind
        for phase in cfg.phases:
            from .fsm import PHASE_ROLE
            role = bind(roles, PHASE_ROLE[phase], 1)
            add('role.' + phase, role.provider in ('claude', 'codex'), role.provider + '/' + role.model)
    except (OSError, KeyError, ValueError) as exc:
        add('roles', False, str(exc))
    try:
        with DB.engine().connect() as c:
            revision = c.scalar(text('SELECT version_num FROM alembic_version'))
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            config = Config()
            config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
            head = ScriptDirectory.from_config(config).get_current_head()
            add('schema', revision == head, f'{revision}; expected {head}; run plat init to upgrade')
            claims = c.scalar(text('SELECT count(*) FROM execution_claims WHERE active AND expires_at < now()'))
            add('execution ownership', claims == 0, f'{claims} expired claim(s) require reconciliation')
    except Exception:
        add('database', False, 'database unavailable or not migrated; check configuration, then run plat init')
    return {'ok': all(c['ok'] for c in checks), 'checks': checks}
