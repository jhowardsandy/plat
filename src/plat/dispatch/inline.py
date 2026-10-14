"""Dependency-aware bounded local execution, one ORM session per worker."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from sqlalchemy import select

from .. import db as DB
from .. import runner
from ..models import Lot, Plat
from ..ownership import OwnershipError


def execute_lot(cfg, plat_id, lot_id, log=print):
    with DB.session() as db:
        p, lot = db.get(Plat, plat_id), db.get(Lot, lot_id)
        try:
            return runner.run_lot(db, cfg, p, lot, cfg.roles(), log=log)
        except (runner.Halt, OwnershipError) as exc:
            db.rollback()
            log(f'{lot.key}: {exc}')
            return 'BLOCKED'


class InlineDispatcher:
    def run(self, cfg, plat_id, *, workers=1, log=print):
        if not 1 <= workers <= 16:
            raise ValueError('workers must be between 1 and 16')
        with DB.session() as db:
            p = db.get(Plat, plat_id)
            if workers > 1 and p.budget_usd > 0:
                raise ValueError('bounded-budget runs use one worker; parallel runs require budget_usd: 0')
        visited, active, states = set(), {}, {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                with DB.session() as db:
                    p = db.get(Plat, plat_id)
                    lots = runner.ordered_lots(db.scalars(select(Lot).where(Lot.plat_id == plat_id)).all())
                    ready = [l for l in lots if l.id not in visited and l.state not in ('DONE', 'ABORTED', 'BLOCKED')
                             and not runner.deps_pending(db, p, l)] if p.status == 'running' else []
                for lot in ready[:max(0, workers - len(active))]:
                    visited.add(lot.id)
                    active[pool.submit(execute_lot, cfg, plat_id, lot.id, log)] = lot.id
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for job in done:
                    states[active.pop(job)] = job.result()
        with DB.session() as db:
            p = db.get(Plat, plat_id)
            runner.maybe_deliver_plat(db, p, log=log, cfg=cfg)
        return states
