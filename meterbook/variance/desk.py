"""Immutable, versioned storage for frozen period variance reports.

A report is calculated from closed invoices and frozen rates, then stored under
a caller-chosen identifier together with the ordered scope parameters.  Stored
payloads are self-describing and digest-protected; a later freeze of the same
scope creates a new monotonic version instead of overwriting history, matching
the rule that subsequent revisions open a new version.
"""
import json

from meterbook.values import canonical, digest, key, prose, reject
from meterbook.variance import analysis

DDL = '''
CREATE TABLE IF NOT EXISTS variance_reports(
    report_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    contract_id TEXT NOT NULL,
    before_period TEXT NOT NULL,
    after_period TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(report_id, version)
);
'''

SCOPE_FIELDS = ('contract_id', 'before_period', 'after_period', 'meter_align', 'meters')


class VarianceDesk:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('variance_transaction', 'Initialize variance desk outside transactions')
        store.db.executescript(DDL)

    def _scope_hash(self, arguments):
        return digest({field: arguments.get(field) for field in SCOPE_FIELDS})

    def freeze(self, report_id, contract_id, before_period, after_period, meter_align=None, meters=None, note=None):
        key(report_id)
        if note is not None:
            note = prose(note, 'note')
        arguments = {
            'contract_id': contract_id,
            'before_period': before_period,
            'after_period': after_period,
            'meter_align': meter_align or {},
            'meters': meters,
        }
        with self.store.atomic():
            existing = self.store.one(
                'SELECT MAX(version) AS version FROM variance_reports WHERE report_id=?',
                (report_id,),
            )
            version = (existing['version'] or 0) + 1
            report = analysis.analyze(
                self.store, contract_id, before_period, after_period,
                meter_align=meter_align, meters=meters, note=note,
            )
            if not report['reconciliation']['conserved']:
                reject('variance_not_conserved', 'Refusing to freeze a report whose factors do not conserve the delta')
            scope_hash = self._scope_hash(arguments)
            self.store.execute(
                'INSERT INTO variance_reports VALUES(?,?,?,?,?,?,?,?,?,?)',
                (
                    report_id, version, contract_id, before_period, after_period,
                    scope_hash, canonical(report), report['digest'], note, self.store.clock(),
                ),
            )
            self.store.log('variance.frozen', {
                'report_id': report_id,
                'version': version,
                'contract_id': contract_id,
                'before_period': before_period,
                'after_period': after_period,
                'digest': report['digest'],
            })
            return self.get(report_id, version)

    def get(self, report_id, version=None):
        key(report_id)
        if version is None:
            row = self.store.one(
                'SELECT * FROM variance_reports WHERE report_id=? ORDER BY version DESC LIMIT 1',
                (report_id,),
            )
        else:
            row = self.store.one(
                'SELECT * FROM variance_reports WHERE report_id=? AND version=?',
                (report_id, version),
            )
        if row is None:
            reject('variance_report_missing', 'Variance report or version does not exist')
        payload = json.loads(row['payload'])
        stored_digest = payload.pop('digest')
        content_digest = digest(payload)
        if stored_digest != row['digest'] or content_digest != row['digest']:
            reject('variance_report_corrupt', 'Stored variance report digest mismatch')
        payload['digest'] = stored_digest
        if self._scope_hash({
            'contract_id': row['contract_id'],
            'before_period': row['before_period'],
            'after_period': row['after_period'],
            'meter_align': payload['alignment']['mapping'],
            'meters': payload['scope']['meters'],
        }) != row['scope_hash']:
            reject('variance_report_corrupt', 'Stored scope hash does not match report parameters')
        return {**{name: row[name] for name in ('report_id', 'version', 'contract_id', 'before_period', 'after_period', 'scope_hash', 'note', 'created_at')}, 'report': payload}

    def versions(self, report_id):
        key(report_id)
        rows = self.store.all(
            'SELECT version, digest, scope_hash, note, created_at, before_period, after_period FROM variance_reports WHERE report_id=? ORDER BY version',
            (report_id,),
        )
        if not rows:
            reject('variance_report_missing', 'Variance report does not exist')
        return rows

    def listing(self):
        return self.store.all(
            'SELECT report_id, version, contract_id, before_period, after_period, digest, created_at FROM variance_reports ORDER BY report_id, version'
        )
