import unittest
from decimal import Decimal
from fractions import Fraction

from meterbook.service import Meterbook
from meterbook.contracts import registry, rates
from meterbook.events import receiver, reconcile, disputes
from meterbook.settlement import invoices
from meterbook.variance import analysis
from meterbook.values import MeterError


def contract_definition(meters):
    return {
        'valid_from': '2026-01-01T00:00:00Z',
        'valid_until': '2027-01-01T00:00:00Z',
        'meters': meters,
        'purposes': ['research'],
        'regions': ['CN'],
        'tolerance': {meter: '0' for meter in meters},
    }


def tariff(price, included='0', minimum='0', maximum=None):
    result = {
        'tiers': [{'up_to': None, 'unit_price': price}],
        'included': included,
        'minimum_charge': minimum,
    }
    if maximum is not None:
        result['maximum_charge'] = maximum
    return result


class VarianceCase(unittest.TestCase):
    contract_id = 'contract-a'

    def setUp(self):
        self.app = Meterbook(clock=lambda: '2026-12-15T00:00:00.000000Z')
        self.store = self.app.store
        for actor, role in (('analyst', 'analyst'), ('approver', 'approver'), ('reviewer', 'approver')):
            disputes.register(self.store, actor, role)

    def tearDown(self):
        self.app.close()

    def create_contract(self, meters):
        registry.create(self.store, self.contract_id, 'provider-a', 'consumer-a', 'CNY', contract_definition(meters))

    def add_rate(self, rate_id, meter, start, end, definition):
        rates.add(self.store, rate_id, self.contract_id, meter, start, end, definition)

    def _next_sequence(self, contract_id, side):
        counters = getattr(self, '_counters', None)
        if counters is None:
            counters = self._counters = {}
        key = (contract_id, side)
        counters[key] = counters.get(key, 0) + 1
        return counters[key]

    def _send_pair(self, contract_id, meter, quantity, period, identity, revision, action):
        for side in ('provider', 'consumer'):
            sequence = self._next_sequence(contract_id, side)
            value = {
                'event_id': f'{side}-{contract_id}-{sequence}-{identity}',
                'contract_id': contract_id,
                'side': side,
                'sequence': sequence,
                'usage_id': identity,
                'revision': revision,
                'action': action,
                'usage': {
                    'meter': meter,
                    'at': f'{period}-10T12:00:00Z',
                    'quantity': str(quantity),
                    'purpose': 'research',
                    'region': 'CN',
                    'dimensions': {'source': 'synthetic'},
                },
            }
            if action != 'report':
                value['reason'] = 'synthetic correction'
            receiver.receive(self.store, value)
            self.assertIsNone(receiver.drain(self.store, contract_id, side)['blocked'])

    def report_usage(self, meter, quantity, period, identity):
        self._send_pair(self.contract_id, meter, quantity, period, identity, 1, 'report')

    def close_period(self, period, usage, run_prefix, contract_id=None):
        contract_id = contract_id or self.contract_id
        for meter, quantity, identity in usage:
            self._send_pair(contract_id, meter, quantity, period, identity, 1, 'report')
        run_id = f'{run_prefix}-run'
        reconcile.run(self.store, run_id, contract_id, period)
        invoice_id = f'{run_prefix}-invoice'
        invoices.prepare(self.store, invoice_id, run_id)
        invoices.close(self.store, invoice_id, 1, 'approver')
        return invoices.get(self.store, invoice_id)

    def adjust_period(self, period, usage_changes, run_prefix, kind='adjustment'):
        for meter, quantity, identity in usage_changes:
            self._send_pair(self.contract_id, meter, quantity, period, identity, 2, 'correct')
        run_id = f'{run_prefix}-run'
        reconcile.run(self.store, run_id, self.contract_id, period)
        invoice_id = f'{run_prefix}-invoice'
        invoices.prepare(self.store, invoice_id, run_id, kind=kind)
        invoices.close(self.store, invoice_id, 1, 'approver')
        return invoices.get(self.store, invoice_id)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(MeterError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class FactorDecomposition(VarianceCase):
    def setUp(self):
        super().setUp()
        # Two meters, one flat tariff per period, so volume/mix/rate are clean.
        self.create_contract(['records', 'calls'])
        self.add_rate('records-old', 'records', '2026-01-01T00:00:00Z', '2026-11-01T00:00:00Z', tariff('0.10'))
        self.add_rate('records-new', 'records', '2026-11-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.20'))
        self.add_rate('calls-old', 'calls', '2026-01-01T00:00:00Z', '2026-11-01T00:00:00Z', tariff('1.00'))
        self.add_rate('calls-new', 'calls', '2026-11-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.50'))
        # October: 100 records + 10 calls -> 1000 + 1000 = 2000 cents (total Q=110).
        self.close_period('2026-10', [
            ('records', '100', 'u-rec-oct'),
            ('calls', '10', 'u-call-oct'),
        ], 'oct')
        # November: 200 records + 20 calls -> 4000 + 1000 = 5000 cents (total Q=220, exact 2x).
        self.close_period('2026-11', [
            ('records', '200', 'u-rec-nov'),
            ('calls', '20', 'u-call-nov'),
        ], 'nov')

    def test_frozen_targets_and_recomputation(self):
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        self.assertTrue(report['periods']['before']['recomputed_matches_frozen'])
        self.assertTrue(report['periods']['after']['recomputed_matches_frozen'])
        self.assertEqual(report['periods']['before']['frozen_total_cents'], 2000)
        self.assertEqual(report['periods']['after']['frozen_total_cents'], 5000)
        # Endpoint counterfactuals reproduce the frozen charges exactly.
        empty = report['counterfactuals'][0]
        full = report['counterfactuals'][-1]
        self.assertEqual(empty['coalition'], [])
        self.assertEqual(sorted(full['coalition']), ['mix', 'rate', 'volume'])
        self.assertEqual(empty['cost_cents'], 2000)
        self.assertEqual(full['cost_cents'], 5000)

    def test_factor_contributions_conserve_to_the_cent(self):
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        factors = {row['factor']: row['contribution_cents'] for row in report['decomposition']['factors']}
        self.assertEqual(sum(factors.values()), report['decomposition']['comparable_delta_cents'])
        self.assertEqual(report['decomposition']['comparable_delta_cents'], 3000)
        self.assertTrue(report['reconciliation']['conserved'])
        self.assertEqual(report['reconciliation']['unexplained_cents'], 0)
        # Per-meter rows also conserve exactly.
        for meter, row in report['decomposition']['per_meter'].items():
            self.assertEqual(sum(row['contributions_cents'].values()), row['delta_cents'])
        # Aggregate equals the sum of the displayed meter rows.
        for factor in analysis.FACTORS:
            self.assertEqual(
                factors[factor],
                sum(row['contributions_cents'][factor] for row in report['decomposition']['per_meter'].values()),
            )

    def test_effects_have_expected_directions(self):
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        factors = {row['factor']: row['contribution_cents'] for row in report['decomposition']['factors']}
        # Structure is unchanged (the two meters double in lockstep), so the
        # mix effect is exactly zero and no volume/rate interaction is dumped
        # onto a factor. The remaining 3000 delta is split symmetrically:
        # volume-only marginal is +2000 at old rates, rate-only is +500 at the
        # old volume, and their interaction is shared equally by Shapley.
        self.assertEqual(factors['mix'], 0)
        self.assertEqual(factors['volume'], 2250)
        self.assertEqual(factors['rate'], 750)
        # Both point upward: volume grew and, on net, the tariff reprice raised cost.
        self.assertGreater(factors['volume'], 0)
        self.assertGreater(factors['rate'], 0)

    def test_symmetric_allocation_does_not_dump_interactions_on_last_factor(self):
        # Change both volumes and mixes so an interaction exists: every factor
        # must equal its six-permutation Shapley mean, not a sequential residual.
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        grid = {frozenset(row['coalition']): row['cost_cents'] for row in report['counterfactuals']}
        from itertools import permutations
        expected = {factor: Fraction(0) for factor in analysis.FACTORS}
        for order in permutations(analysis.FACTORS):
            active = frozenset()
            previous = grid[active]
            for factor in order:
                active = active | {factor}
                expected[factor] += Fraction(grid[active] - previous)
                previous = grid[active]
        for factor in analysis.FACTORS:
            exact = Fraction(next(row['exact_fraction'] for row in report['decomposition']['factors'] if row['factor'] == factor))
            self.assertEqual(exact, expected[factor] / 6)

    def test_swap_periods_reverses_every_contribution(self):
        forward = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        reverse = analysis.analyze(self.store, 'contract-a', '2026-11', '2026-10')
        for factor in analysis.FACTORS:
            a = next(row['contribution_cents'] for row in forward['decomposition']['factors'] if row['factor'] == factor)
            b = next(row['contribution_cents'] for row in reverse['decomposition']['factors'] if row['factor'] == factor)
            self.assertEqual(a, -b)
        self.assertEqual(
            forward['reconciliation']['consumption_delta_cents'],
            -reverse['reconciliation']['consumption_delta_cents'],
        )
        for meter, row in forward['decomposition']['per_meter'].items():
            for factor in analysis.FACTORS:
                self.assertEqual(
                    row['contributions_cents'][factor],
                    -reverse['decomposition']['per_meter'][meter]['contributions_cents'][factor],
                )

    def test_mix_effect_appears_when_structure_shifts(self):
        # A separate contract where the cheap meter gains share at constant volume.
        registry.create(self.store, 'contract-b', 'provider-b', 'consumer-b', 'CNY', contract_definition(['records', 'calls']))
        rates.add(self.store, 'b-records', 'contract-b', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('1.00'))
        rates.add(self.store, 'b-calls', 'contract-b', 'calls', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        # Same total quantity 100 in both months; only the meter mix changes.
        self.close_period('2026-10', [('records', '50', 'b-r-o'), ('calls', '50', 'b-c-o')], 'b-oct', contract_id='contract-b')
        self.close_period('2026-11', [('records', '10', 'b-r-n'), ('calls', '90', 'b-c-n')], 'b-nov', contract_id='contract-b')
        report = analysis.analyze(self.store, 'contract-b', '2026-10', '2026-11')
        factors = {row['factor']: row['contribution_cents'] for row in report['decomposition']['factors']}
        self.assertEqual(factors['volume'], 0)
        self.assertEqual(factors['rate'], 0)
        # Shift of 40 units from the 1.00 meter to the 0.10 meter -> -3600 cents.
        self.assertEqual(factors['mix'], -3600)
        self.assertEqual(report['reconciliation']['consumption_delta_cents'], -3600)


class StructuralAlignment(VarianceCase):
    def test_new_and_retired_meters_keep_full_amount(self):
        # October contract declares records + archive; November revises the
        # contract to retire archive and introduce calls instead.
        registry.create(self.store, 'contract-a', 'provider-a', 'consumer-a', 'CNY',
                         contract_definition(['records', 'archive']))
        rates.add(self.store, 'r-rate', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        rates.add(self.store, 'a-rate', 'contract-a', 'archive', '2026-01-01T00:00:00Z', '2026-11-01T00:00:00Z', tariff('0.50'))
        # October: records 100 (1000c) + archive 10 (500c).
        self.close_period('2026-10', [('records', '100', 'r-oct'), ('archive', '10', 'a-oct')], 'oct')
        # Retire archive: withdraw its rate, then revise the contract meter list.
        rates.withdraw(self.store, 'a-rate', 'archive meter retired')
        registry.revise(
            self.store, 'contract-a', 1,
            contract_definition(['records', 'calls']), 'retire archive, add calls',
        )
        rates.add(self.store, 'c-rate', 'contract-a', 'calls', '2026-11-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('1.00'))
        # November: records 200 (2000c) + calls 5 (500c).
        self.close_period('2026-11', [('records', '200', 'r-nov'), ('calls', '5', 'c-nov')], 'nov')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        statuses = {row['meter']: row['status'] for row in report['alignment']['structural']}
        self.assertEqual(statuses, {'archive': 'retired', 'calls': 'added'})
        structural = {row['meter']: row['delta_cents'] for row in report['alignment']['structural']}
        self.assertEqual(structural, {'archive': -500, 'calls': 500})
        # Only records entered the Shapley decomposition; no fabricated zero prices.
        self.assertEqual(report['decomposition']['comparable_meters'], ['records'])
        self.assertEqual(report['decomposition']['comparable_delta_cents'], 1000)
        self.assertEqual(report['decomposition']['structural_delta_cents'], 0)
        self.assertTrue(report['reconciliation']['conserved'])
        self.assertEqual(report['reconciliation']['consumption_delta_cents'], 1000)
        for row in report['alignment']['structural']:
            self.assertIn('保留', row['note'])

    def test_explicit_rename_alignment_makes_meters_comparable(self):
        self.create_contract(['records', 'records_v2'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        rates.add(self.store, 'r2', 'contract-a', 'records_v2', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        self.close_period('2026-10', [('records', '100', 'old')], 'oct')
        self.close_period('2026-11', [('records_v2', '100', 'new')], 'nov')
        # Without mapping each renamed meter has a zero-quantity side, so the
        # pair is structural (amounts retained) rather than priced at zero.
        raw = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        self.assertEqual({row['reason'] for row in raw['alignment']['structural']}, {'zero_quantity_side'})
        self.assertEqual(raw['alignment']['comparable'], [])
        # With an explicit alignment they become one comparable pair.
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11', meter_align={'records_v2': 'records'})
        self.assertEqual(report['alignment']['structural'], [])
        pair = report['alignment']['comparable'][0]
        self.assertEqual(pair['alignment'], 'aligned_rename')
        self.assertEqual(pair['before_meter'], 'records')
        self.assertEqual(pair['after_meter'], 'records_v2')
        self.assertEqual(pair['delta_cents'], 0)
        self.assertTrue(report['reconciliation']['conserved'])

    def test_zero_quantity_side_is_structural_not_zero_priced(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10', minimum='5.00'))
        # October has a minimum-charge line with zero quantity; November bills real usage.
        # Minimum charge only applies when quantity > 0, so use paired records instead:
        # October quantity 1 bills the 5.00 minimum (500c), November zero usage means no line.
        self.close_period('2026-10', [('records', '1', 'r-oct')], 'oct')
        # November: no usage at all -> empty invoice chain total 0 with no records line.
        reconcile.run(self.store, 'nov-run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'nov-invoice', 'nov-run')
        invoices.close(self.store, 'nov-invoice', 1, 'approver')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        self.assertEqual(report['decomposition']['comparable_meters'], [])
        structural = report['alignment']['structural'][0]
        self.assertEqual(structural['reason'], 'zero_quantity_side')
        self.assertEqual(structural['delta_cents'], -500)
        self.assertEqual(report['reconciliation']['consumption_delta_cents'], -500)
        self.assertTrue(report['reconciliation']['conserved'])


class AdjustmentsAndCredits(VarianceCase):
    def test_credit_memo_and_retroactive_adjustment_are_separate(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        self.close_period('2026-10', [('records', '100', 'r-oct')], 'oct')  # 1000 cents
        # Retroactive correction inside October: 120 records -> 1200, adjustment +200.
        self.adjust_period('2026-10', [('records', '120', 'r-oct')], 'oct-adj')
        self.close_period('2026-11', [('records', '200', 'r-nov')], 'nov')  # 2000 cents
        # A credit memo on November after close: down to 150 -> 1500, adjustment -500.
        self.adjust_period('2026-11', [('records', '150', 'r-nov')], 'nov-adj')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        before_adj = report['adjustments']['before']
        after_adj = report['adjustments']['after']
        self.assertEqual(before_adj['retroactive_adjustment_cents'], 200)
        self.assertEqual(after_adj['credit_memo_cents'], -500)
        self.assertEqual(before_adj['original_invoice_cents'], 1000)
        # The decomposition compares frozen period totals (1200 vs 1500), never the adjustments.
        self.assertEqual(report['periods']['before']['frozen_total_cents'], 1200)
        self.assertEqual(report['periods']['after']['frozen_total_cents'], 1500)
        self.assertEqual(report['reconciliation']['consumption_delta_cents'], 300)
        # Credit/retroactive entries are explicitly excluded from factor totals.
        factor_total = sum(row['contribution_cents'] for row in report['decomposition']['factors'])
        self.assertEqual(factor_total, 300)
        self.assertTrue(report['reconciliation']['chain_bridges_balanced'])
        # The adjustment invoice list is present and classified, never silently dropped.
        self.assertEqual({e['classification'] for e in before_adj['entries']}, {'retroactive_adjustment'})
        self.assertEqual({e['classification'] for e in after_adj['entries']}, {'credit_memo'})

    def test_credit_application_is_reported_not_counted_as_consumption(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        self.close_period('2026-10', [('records', '100', 'r-oct')], 'oct')
        # Negative credit memo in October.
        self.adjust_period('2026-10', [('records', '50', 'r-oct')], 'oct-credit')
        self.close_period('2026-11', [('records', '200', 'r-nov')], 'nov')
        self.app.credits.apply('apply-1', 'oct-credit-invoice', 'nov-invoice', 300, 'approver', 'apply credit')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        self.assertEqual(report['adjustments']['before']['credit_memo_cents'], -500)
        applied = report['adjustments']['after']['credit_applications']
        self.assertEqual(applied, [{'invoice_id': 'nov-invoice', 'credit_applied_cents': 300}])
        # Application changes neither the frozen consumption total nor the delta.
        self.assertEqual(report['reconciliation']['consumption_delta_cents'], 2000 - 500)


class ScopeAndValidation(VarianceCase):
    def test_meter_scope_filters_and_excludes(self):
        self.create_contract(['records', 'calls'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        rates.add(self.store, 'c1', 'contract-a', 'calls', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('1.00'))
        self.close_period('2026-10', [('records', '100', 'r-o'), ('calls', '10', 'c-o')], 'oct')
        self.close_period('2026-11', [('records', '200', 'r-n'), ('calls', '20', 'c-n')], 'nov')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11', meters=['records'])
        scoped = report['scoped_decomposition']
        self.assertEqual(scoped['comparable_delta_cents'], 1000)
        self.assertEqual(scoped['factor_contribution_cents']['volume'], 1000)
        self.assertEqual(scoped['explained_delta_cents'], 1000)
        self.assertEqual([row['meter'] for row in report['scope']['excluded_meters']], ['calls'])
        # Full reconciliation still covers the whole bill regardless of scope.
        self.assertTrue(report['reconciliation']['conserved'])

    def test_rejects_unclosed_period_and_bad_inputs(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        self.close_period('2026-10', [('records', '100', 'r-o')], 'oct')
        # November is not closed, so analysis of that period is rejected.
        self.error('variance_unclosed', analysis.analyze, self.store, 'contract-a', '2026-10', '2026-11')
        self.error('variance_periods', analysis.analyze, self.store, 'contract-a', '2026-10', '2026-10')
        # Close November, then scope and alignment validation can be exercised.
        self.close_period('2026-11', [('records', '200', 'r-n')], 'nov')
        self.error('variance_scope', analysis.analyze, self.store, 'contract-a', '2026-10', '2026-11', meters=['nope'])
        self.error('variance_alignment', analysis.analyze, self.store, 'contract-a', '2026-10', '2026-11', meter_align={'nope': 'records'})

    def test_currency_is_pinned_to_contract(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        self.close_period('2026-10', [('records', '100', 'a-o')], 'oct')
        self.close_period('2026-11', [('records', '200', 'a-n')], 'nov')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        # The whole report inherits the single contract currency; no FX mixing occurs.
        self.assertEqual(report['currency'], 'CNY')
        self.assertEqual(report['periods']['before']['frozen_total_cents'], 1000)
        self.assertEqual(report['periods']['after']['frozen_total_cents'], 2000)


class MinimumChargeAndRounding(VarianceCase):
    def test_minimum_charge_is_honored_in_counterfactuals(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r-old', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2026-11-01T00:00:00Z', tariff('1.00', minimum='10.00'))
        rates.add(self.store, 'r-new', 'contract-a', 'records', '2026-11-01T00:00:00Z', '2027-01-01T00:00Z', tariff('1.00', minimum='20.00'))
        # 5 units each period: minimum charges 1000 -> 2000 cents.
        self.close_period('2026-10', [('records', '5', 'r-o')], 'oct')
        self.close_period('2026-11', [('records', '5', 'r-n')], 'nov')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        factors = {row['factor']: row['contribution_cents'] for row in report['decomposition']['factors']}
        self.assertEqual(factors['volume'], 0)
        self.assertEqual(factors['mix'], 0)
        self.assertEqual(factors['rate'], 1000)
        # Every intermediate coalition that keeps positive quantity applies the minimum.
        for row in report['counterfactuals']:
            if row['coalition']:
                self.assertGreaterEqual(row['cost_cents'], 1000)
        self.assertTrue(report['reconciliation']['conserved'])

    def test_tiered_tariff_with_included_quantity_is_recomputed(self):
        # Two tiers plus an included free block; the same tariff both periods,
        # so the entire delta must be a pure (tier-aware) volume effect.
        self.create_contract(['records'])
        definition = {
            'tiers': [
                {'up_to': '100', 'unit_price': '0.50'},
                {'up_to': None, 'unit_price': '0.20'},
            ],
            'included': '20',
            'minimum_charge': '0',
        }
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', definition)
        # October: 120 units, 20 included -> 100 billable all in the first tier
        # (bound 100) at 0.50 = 5000 cents.
        self.close_period('2026-10', [('records', '120', 'r-o')], 'oct')
        # November: 220 units, 20 included -> 100 at 0.50 + 100 at 0.20 = 7000 cents.
        self.close_period('2026-11', [('records', '220', 'r-n')], 'nov')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        self.assertEqual(report['periods']['before']['frozen_total_cents'], 5000)
        self.assertEqual(report['periods']['after']['frozen_total_cents'], 7000)
        factors = {row['factor']: row['contribution_cents'] for row in report['decomposition']['factors']}
        self.assertEqual(factors['mix'], 0)
        self.assertEqual(factors['rate'], 0)
        self.assertEqual(factors['volume'], 2000)
        self.assertTrue(report['reconciliation']['conserved'])

    def test_intra_period_rate_switch_produces_multiple_blocks(self):
        # A mid-month tariff change means one meter maps to two rate blocks;
        # usage on each side of the switch stays separately attributable.
        self.create_contract(['records'])
        rates.add(self.store, 'r-old', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2026-11-15T00:00:00Z', tariff('1.00'))
        rates.add(self.store, 'r-new', 'contract-a', 'records', '2026-11-15T00:00:00Z', '2027-01-01T00:00:00Z', tariff('2.00'))
        # October entirely under the old rate: 10 units on the 10th -> 1000 cents.
        self.close_period('2026-10', [('records', '10', 'r-o')], 'oct')
        # November: one usage before and one after the switch.
        self._send_pair('contract-a', 'records', '10', '2026-11', 'r-n-early', 1, 'report')
        # Override the timestamp for the post-switch usage.
        for side in ('provider', 'consumer'):
            sequence = self._next_sequence('contract-a', side)
            receiver.receive(self.store, {
                'event_id': f'{side}-contract-a-{sequence}-r-n-late',
                'contract_id': 'contract-a', 'side': side, 'sequence': sequence,
                'usage_id': 'r-n-late', 'revision': 1, 'action': 'report',
                'usage': {'meter': 'records', 'at': '2026-11-20T12:00:00Z', 'quantity': '10',
                          'purpose': 'research', 'region': 'CN', 'dimensions': {'source': 'synthetic'}},
            })
            receiver.drain(self.store, 'contract-a', side)
        reconcile.run(self.store, 'nov-run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'nov-invoice', 'nov-run')
        invoices.close(self.store, 'nov-invoice', 1, 'approver')
        report = analysis.analyze(self.store, 'contract-a', '2026-10', '2026-11')
        after_view = report['periods']['after']
        self.assertEqual(after_view['frozen_total_cents'], 3000)  # 10*1.00 + 10*2.00
        # The single comparable meter still conserves across all three factors.
        meter_row = report['decomposition']['per_meter']['records']
        self.assertEqual(sum(meter_row['contributions_cents'].values()), meter_row['delta_cents'])
        self.assertEqual(meter_row['delta_cents'], 2000)
        self.assertTrue(report['reconciliation']['conserved'])


class ServiceRouting(VarianceCase):
    def test_analyze_and_freeze_through_service_routes(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        self.close_period('2026-10', [('records', '100', 'r-o')], 'oct')
        self.close_period('2026-11', [('records', '200', 'r-n')], 'nov')
        report = self.app.execute({
            'operation': 'variance.analyze',
            'arguments': {'contract_id': 'contract-a', 'before_period': '2026-10', 'after_period': '2026-11'},
        })
        self.assertEqual(report['method'], 'shapley_three_factor_symmetric_allocation')
        frozen = self.app.execute({
            'operation': 'variance_desk.freeze',
            'arguments': {'report_id': 'rpt-1', 'contract_id': 'contract-a',
                          'before_period': '2026-10', 'after_period': '2026-11', 'note': 'monthly bridge'},
        })
        self.assertEqual(frozen['version'], 1)
        fetched = self.app.execute({
            'operation': 'variance_desk.get',
            'arguments': {'report_id': 'rpt-1'},
        })
        self.assertEqual(fetched['report']['digest'], frozen['report']['digest'])
        # Unknown operation is still rejected.
        self.error('operation', self.app.execute, {'operation': 'variance.nope'})


class VersionFreezing(VarianceCase):
    def test_freeze_versions_are_immutable_and_digest_protected(self):
        self.create_contract(['records'])
        rates.add(self.store, 'r1', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.10'))
        self.close_period('2026-10', [('records', '100', 'r-o')], 'oct')
        self.close_period('2026-11', [('records', '200', 'r-n')], 'nov')
        first = self.app.variance.freeze('vr-1', 'contract-a', '2026-10', '2026-11', note='first freeze')
        self.assertEqual(first['version'], 1)
        second = self.app.variance.freeze('vr-1', 'contract-a', '2026-10', '2026-11')
        self.assertEqual(second['version'], 2)
        self.assertEqual(self.app.variance.get('vr-1')['version'], 2)
        self.assertEqual(self.app.variance.get('vr-1', 1)['version'], 1)
        versions = self.app.variance.versions('vr-1')
        self.assertEqual([row['version'] for row in versions], [1, 2])
        # Tampering with a stored payload is detected on read.
        row = self.store.one("SELECT payload FROM variance_reports WHERE report_id='vr-1' AND version=1")
        import json
        from meterbook.values import canonical
        payload = json.loads(row['payload'])
        payload['note'] = 'tampered'
        self.store.execute("UPDATE variance_reports SET payload=? WHERE report_id='vr-1' AND version=1", (canonical(payload),))
        self.error('variance_report_corrupt', self.app.variance.get, 'vr-1', 1)
        # Listing surfaces both versions.
        self.assertEqual(len(self.app.variance.listing()), 2)


if __name__ == '__main__':
    unittest.main()
