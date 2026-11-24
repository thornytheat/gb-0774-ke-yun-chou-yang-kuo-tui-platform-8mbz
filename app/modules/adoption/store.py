"""排班与接用台账的持久化。

排班文档只存当前排班（`current`）。每次接用以当前排班对补正重新排队，
已生效的组不再出现在待接用清单，失败组回滚后仍可重试；已接用组另由
adoption_log 挡住，不重复生效。每组在一个事务内读改写，改层两端与两层
人数要么一起生效，要么一起回滚。
"""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.errors import SurveyProblem

from .plan import apply_component, satisfied


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def _dump(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


class ScheduleStore:
    """与扩推归档共用同一个 SQLite 文件，另立排班表。"""

    def __init__(self, filename):
        self.filename = filename
        Path(filename).parent.mkdir(exist_ok=True, parents=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schedules(
                    id TEXT PRIMARY KEY,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adoption_log(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id TEXT NOT NULL,
                    run TEXT NOT NULL,
                    item TEXT NOT NULL,
                    created TEXT NOT NULL,
                    UNIQUE(schedule_id,run,item)
                );
            ''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.filename, timeout=10)) as db, db:
            db.execute('PRAGMA foreign_keys=ON')
            yield db

    @contextmanager
    def transaction(self):
        """写事务：先取写锁，读改写之间不被其他接用插队。"""
        with closing(sqlite3.connect(self.filename, timeout=10, isolation_level=None)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.execute('ROLLBACK')
                raise
            else:
                db.execute('COMMIT')

    def exists(self, schedule_id):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM schedules WHERE id=?', (schedule_id,)).fetchone() is not None

    def create(self, schedule_id, document):
        """建立排班基准；重复建立（同源重试）返回既有文档而不覆盖。"""
        stamp = _now()
        with self.transaction() as db:
            row = db.execute('SELECT document FROM schedules WHERE id=?', (schedule_id,)).fetchone()
            if row:
                return json.loads(row[0])
            db.execute('INSERT INTO schedules VALUES (?,?,?,?)',
                       (schedule_id, stamp, stamp, _dump(document)))
        return document

    def read(self, schedule_id):
        with self.connect() as db:
            row = db.execute('SELECT created,updated,document FROM schedules WHERE id=?',
                             (schedule_id,)).fetchone()
        if not row:
            raise SurveyProblem('NO_SCHEDULE', '该排班尚未建立', 404)
        created, updated, document = row
        return dict(id=schedule_id, created=created, updated=updated,
                    document=json.loads(document))

    def mark_departed(self, schedule_id, layers):
        stamp = _now()
        with self.transaction() as db:
            row = db.execute('SELECT document FROM schedules WHERE id=?', (schedule_id,)).fetchone()
            if not row:
                raise SurveyProblem('NO_SCHEDULE', '该排班尚未建立', 404)
            document = json.loads(row[0])
            unknown = sorted(set(layers) - set(document['current']['layers']))
            if unknown:
                raise SurveyProblem('NO_LAYER', f'排班中没有分层：{"、".join(unknown)}', 404)
            for name in layers:
                document['current']['layers'][name]['departed'] = True
            db.execute('UPDATE schedules SET document=?, updated=? WHERE id=?',
                       (_dump(document), stamp, schedule_id))
        return document

    def adopt(self, schedule_id, run_key, component, after):
        """在一个事务内落一组调整并记账；失败整组回滚，两端都保留旧人数。

        返回 True 表示本次写入，False 表示排班现况已具备目标值、跳过重复生效。
        """
        stamp = _now()
        item = component['item']
        with self.transaction() as db:
            row = db.execute('SELECT document FROM schedules WHERE id=?', (schedule_id,)).fetchone()
            if not row:
                raise SurveyProblem('NO_SCHEDULE', '该排班尚未建立', 404)
            document = json.loads(row[0])
            if satisfied(document['current'], component, after):
                return False
            apply_component(document['current'], component, after)
            document.setdefault('adoptions', []).append(
                dict(run=run_key, item=item, layers=list(component['layers']), created=stamp))
            db.execute('UPDATE schedules SET document=?, updated=? WHERE id=?',
                       (_dump(document), stamp, schedule_id))
            db.execute('INSERT OR IGNORE INTO adoption_log(schedule_id,run,item,created) VALUES (?,?,?,?)',
                       (schedule_id, run_key, item, stamp))
        return True

    def clear(self, schedule_id):
        with self.transaction() as db:
            db.execute('DELETE FROM adoption_log WHERE schedule_id=?', (schedule_id,))
            db.execute('DELETE FROM schedules WHERE id=?', (schedule_id,))

    def history(self, schedule_id):
        with self.connect() as db:
            rows = db.execute('SELECT run,item,created FROM adoption_log WHERE schedule_id=? '
                              'ORDER BY seq', (schedule_id,)).fetchall()
        return [dict(run=run, item=item, created=created) for run, item, created in rows]


def empty_document(origin, profile, departed):
    departed = set(departed)
    return dict(
        origin=origin,
        created=_now(),
        current=dict(
            layers={name: dict(integers=dict(row['integers']), unknown=row['unknown'],
                               tickets=row['tickets'], departed=name in departed)
                    for name, row in profile['layers'].items()},
            trips={trip: dict(layer=row['layer'], tickets=row['tickets'])
                   for trip, row in profile['trips'].items()},
        ),
        adoptions=[],
    )
