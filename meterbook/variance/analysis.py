"""Closed-period charge variance analysis with symmetric factor allocation.

The analysis is read-only and works exclusively from *frozen* invoice
snapshots: closed invoice chains pin the billed amounts, while their frozen
calculations pin quantities, rate identifiers and per-line charges.  Every
frozen line charge is recomputed from its frozen rate definition and its
digest is checked before use, so the target amount shown in a report is always
independently reproducible from the freeze.

Three factors explain the consumption delta of comparable meters:

* ``volume`` - total billed quantity moves between periods;
* ``mix``    - the fixed total volume is redistributed across meters;
* ``rate``   - frozen tariffs (tiers, included quantities, minimum and
                maximum charges, effective intervals) change.

Factor contributions are Shapley values over every three-factor permutation,
so interaction terms are shared symmetrically instead of being assigned to the
last switched factor.  Exact rational arithmetic carries every marginal mean;
only counterfactual quantities are quantized to the same six-decimal scale as
recorded usage, and only the final per-meter factor values are rounded to minor
currency units using the largest-remainder rule.  The rounded contributions
therefore sum, to the cent, to the comparable delta.

Meters that exist in only one period, or that have a zero-quantity side, keep
their full amount in a separate alignment section and are never priced through
a fabricated zero price.  Credit memos and retroactive adjustments come from
the invoice chain and are attributed separately; they never enter the
consumption decomposition.
"""
import json
import math
from decimal import Decimal, ROUND_HALF_EVEN
from fractions import Fraction
from itertools import combinations, permutations

from meterbook.values import digest, encoded, key, month, prose, reject
from meterbook.contracts import rates, registry
from meterbook.settlement import invoices, pricing
from meterbook.settlement.credits import applied_credit

FACTORS = ('volume', 'mix', 'rate')
FACTOR_LABELS = {
    'volume': '总用量效应',
    'mix': '计量项结构效应',
    'rate': '费率效应',
}
QUANTITY_SCALE = Decimal('0.000001')
COALITIONS = tuple(
    frozenset(members)
    for length in range(len(FACTORS) + 1)
    for members in combinations(FACTORS, length)
)
EMPTY = frozenset()
FULL = frozenset(FACTORS)


def _fraction(decimal_value):
    return Fraction(decimal_value)


def _decimal(fraction_value):
    return Decimal(fraction_value.numerator) / Decimal(fraction_value.denominator)


def _quantized(fraction_value):
    return _decimal(fraction_value).quantize(QUANTITY_SCALE, rounding=ROUND_HALF_EVEN)


def _largest_remainder(exact_by_factor, total_cents):
    """Round signed exact fractions so the integer parts sum to the total."""
    floors = {factor: math.floor(value) for factor, value in exact_by_factor.items()}
    leftover = total_cents - sum(floors.values())
    remainders = sorted(
        exact_by_factor,
        key=lambda factor: (-(exact_by_factor[factor] - floors[factor]), factor),
    )
    result = dict(floors)
    for factor in remainders[:leftover]:
        result[factor] += 1
    return result


def _frozen_contract(store, contract_id, revision):
    row = store.one(
        'SELECT payload FROM contract_history WHERE contract_id=? AND revision=?',
        (contract_id, revision),
    )
    if row is None:
        reject('variance_contract_revision', 'Frozen contract revision is missing from history', contract_id=contract_id, revision=revision)
    snapshot = json.loads(row['payload'])
    return snapshot['definition'] if 'definition' in snapshot else snapshot


def _period_view(store, contract_id, period):
    """Load one closed period and independently recompute every frozen line."""
    month(period)
    chain = invoices.chain(store, contract_id, period)
    closed = chain['invoices']
    if not closed:
        reject('variance_unclosed', 'Period has no closed invoice chain', contract_id=contract_id, period=period)
    if any(invoice['state'] != 'closed' for invoice in closed):
        reject('variance_not_closed', 'Variance analysis requires fully closed invoice chains', period=period)
    head = closed[-1]
    calculation = head['snapshot']['calculation']
    # Use the contract revision pinned at freeze time, not the current one, so
    # meters retired or introduced by a later revision stay period-correct.
    frozen_contract = _frozen_contract(store, contract_id, calculation['contract_revision'])
    meters = {meter: [] for meter in frozen_contract['meters']}
    recomputed_total = 0
    for line in calculation['lines']:
        rate = rates.get(store, line['rate_id'])
        if rate['digest'] != line['rate_digest']:
            reject('variance_rate_drift', 'Frozen rate digest no longer matches stored rate', rate_id=line['rate_id'])
        quantity = Decimal(line['pricing']['quantity'])
        checked = pricing.tier_charge(encoded(quantity), rate['definition'])
        if checked['amount_cents'] != line['amount_cents'] or checked['amount_cents'] != line['pricing']['amount_cents']:
            reject('variance_recompute', 'Frozen line charge cannot be reproduced', line_id=line['line_id'])
        recomputed_total += line['amount_cents']
        meters.setdefault(line['meter'], []).append({
            'line_id': line['line_id'],
            'rate_id': line['rate_id'],
            'rate_digest': line['rate_digest'],
            'interval': line['interval'],
            'quantity': encoded(quantity),
            'amount_cents': line['amount_cents'],
            'definition': rate['definition'],
        })
    if recomputed_total != calculation['total_cents']:
        reject('variance_recompute', 'Recomputed lines do not match frozen period total', period=period)
    bridge = []
    running = 0
    for invoice in closed:
        running += invoice['total_cents']
        bridge.append({
            'invoice_id': invoice['invoice_id'],
            'kind': invoice['kind'],
            'parent_id': invoice['parent_id'],
            'amount_cents': invoice['total_cents'],
            'running_cents': running,
        })
    return {
        'period': period,
        'head_invoice_id': head['invoice_id'],
        'head_digest': head['digest'],
        'chain': bridge,
        'frozen_total_cents': calculation['total_cents'],
        'recomputed_total_cents': recomputed_total,
        'recomputed_matches_frozen': recomputed_total == calculation['total_cents'],
        'rates_fingerprint': calculation['rates_fingerprint'],
        'run_digest': calculation['run_digest'],
        'meters': meters,
    }


def _adjustments(store, view):
    entries = []
    credit_memo_cents = 0
    retroactive_cents = 0
    for step in view['chain']:
        if step['kind'] != 'adjustment':
            continue
        invoice = invoices.get(store, step['invoice_id'])
        classification = 'credit_memo' if invoice['total_cents'] < 0 else 'retroactive_adjustment'
        lines = [{
            'line_id': line['line_id'],
            'before_cents': line['before_cents'],
            'after_cents': line['after_cents'],
            'delta_cents': line['amount_cents'],
        } for line in invoice['snapshot']['lines']]
        entries.append({
            'invoice_id': invoice['invoice_id'],
            'period': invoice['period'],
            'parent_id': invoice['parent_id'],
            'classification': classification,
            'amount_cents': invoice['total_cents'],
            'lines': lines,
        })
        if classification == 'credit_memo':
            credit_memo_cents += invoice['total_cents']
        else:
            retroactive_cents += invoice['total_cents']
    applications = []
    for invoice_id in (step['invoice_id'] for step in view['chain']):
        incoming = applied_credit(store, invoice_id)
        if incoming:
            applications.append({'invoice_id': invoice_id, 'credit_applied_cents': incoming})
    original_cents = view['chain'][0]['amount_cents'] if view['chain'] and view['chain'][0]['kind'] == 'original' else 0
    balanced = original_cents + retroactive_cents + credit_memo_cents == view['frozen_total_cents']
    return {
        'entries': entries,
        'credit_memo_cents': credit_memo_cents,
        'retroactive_adjustment_cents': retroactive_cents,
        'credit_applications': applications,
        'original_invoice_cents': original_cents,
        'bridge_balanced': balanced,
    }


def _meter_totals(view):
    return {
        meter: {
            'quantity_fraction': sum((_fraction(Decimal(block['quantity'])) for block in blocks), Fraction(0)),
            'amount_cents': sum(block['amount_cents'] for block in blocks),
            'rate_digests': sorted({block['rate_digest'] for block in blocks}),
            'rate_ids': sorted({block['rate_id'] for block in blocks}),
        }
        for meter, blocks in view['meters'].items()
    }


def _build_alignment(before_meters, after_meters, meter_align):
    """Return (pair list, standalone entries) following explicit alignment.

    Every declared meter exists in both period views (zero-usage periods keep
    an empty block list), so a genuinely added or retired meter is one absent
    from the other period's contract declaration rather than merely unbilled.
    """
    pairs = []
    used_before, used_after = set(), set()
    for after_meter, before_meter in sorted(meter_align.items()):
        if after_meter not in after_meters or before_meter not in before_meters:
            reject('variance_alignment', 'Aligned meter is absent from its period view', after_meter=after_meter, before_meter=before_meter)
        if after_meter in used_after or before_meter in used_before:
            reject('variance_alignment', 'A meter can participate in at most one aligned pair')
        used_after.add(after_meter)
        used_before.add(before_meter)
        pairs.append((before_meter, after_meter, 'aligned_rename'))
    for meter in sorted((before_meters & after_meters) - used_before - used_after):
        pairs.append((meter, meter, 'same_key'))
    standalone = []
    for meter in sorted(after_meters - used_after - before_meters):
        standalone.append((meter, 'added'))
    for meter in sorted(before_meters - used_before - after_meters):
        standalone.append((meter, 'retired'))
    return pairs, standalone


def _block_shares(blocks):
    total = sum((_fraction(Decimal(block['quantity'])) for block in blocks), Fraction(0))
    if total == 0:
        return total, [Fraction(0) for _ in blocks]
    return total, [_fraction(Decimal(block['quantity'])) / total for block in blocks]


def _counterfactual_cost(view_b, view_a, pair_meters, coalition):
    """Evaluate one factor coalition across all comparable meters.

    The three factor dimensions are fully independent:

    * global volume comes from the period selected by the ``volume`` factor;
    * each meter's share of that volume comes from the ``mix`` period;
    * within a meter, rate blocks and their shares come from the ``rate``
      period, so the frozen minimum/maximum and tier rules apply unchanged.
    """
    volume_after = 'volume' in coalition
    mix_after = 'mix' in coalition
    rate_after = 'rate' in coalition
    volume_view = view_a if volume_after else view_b
    mix_view = view_a if mix_after else view_b
    rate_view = view_a if rate_after else view_b
    global_volume = sum(
        (item['quantity_fraction'] for item in _meter_totals(volume_view).values()),
        Fraction(0),
    )
    mix_totals = _meter_totals(mix_view)
    global_mix = sum((item['quantity_fraction'] for item in mix_totals.values()), Fraction(0))
    total_cents = 0
    grid = []
    for pair_index, (before_meter, after_meter, _) in enumerate(pair_meters):
        rate_meter = after_meter if rate_after else before_meter
        mix_meter = after_meter if mix_after else before_meter
        rate_blocks = rate_view['meters'][rate_meter]
        _, rate_shares = _block_shares(rate_blocks)
        meter_share = mix_totals[mix_meter]['quantity_fraction'] / global_mix
        meter_quantity = global_volume * meter_share
        meter_cents = 0
        block_rows = []
        for block, block_share in zip(rate_blocks, rate_shares):
            block_fraction = meter_quantity * block_share
            quantity = _quantized(block_fraction)
            charged = pricing.tier_charge(encoded(quantity), block['definition'])
            meter_cents += charged['amount_cents']
            block_rows.append({
                'line_id': block['line_id'],
                'rate_id': block['rate_id'],
                'rate_side_meter': rate_meter,
                'quantity': encoded(quantity),
                'amount_cents': charged['amount_cents'],
            })
        total_cents += meter_cents
        grid.append({'pair_index': pair_index, 'meter': rate_meter, 'blocks': block_rows, 'meter_cents': meter_cents})
    return total_cents, grid


def _shapley(cost_by_coalition):
    sums = {factor: Fraction(0) for factor in FACTORS}
    for order in permutations(FACTORS):
        active = EMPTY
        previous = cost_by_coalition[active]
        for factor in order:
            active = active | {factor}
            sums[factor] += Fraction(cost_by_coalition[active] - previous)
            previous = cost_by_coalition[active]
    return {factor: value / len(list(permutations(FACTORS))) for factor, value in sums.items()}


def analyze(store, contract_id, before_period, after_period, meter_align=None, meters=None, note=None):
    """Compare two closed periods and return a conserved variance report."""
    key(contract_id)
    month(before_period)
    month(after_period)
    if before_period == after_period:
        reject('variance_periods', 'Comparison needs two distinct closed periods')
    contract = registry.get(store, contract_id)
    if meter_align is None:
        meter_align = {}
    if not isinstance(meter_align, dict):
        reject('variance_alignment', 'meter_align must be an after-to-before meter mapping')
    for after_meter, before_meter in meter_align.items():
        key(after_meter, 'after_meter')
        key(before_meter, 'before_meter')
    if meters is not None:
        if not isinstance(meters, list) or not meters or len(meters) != len(set(meters)):
            reject('variance_scope', 'meters scope must be a nonempty list of distinct keys')
        meters = [key(meter, 'meter') for meter in meters]
    if note is not None:
        note = prose(note, 'note')

    view_b = _period_view(store, contract_id, before_period)
    view_a = _period_view(store, contract_id, after_period)
    # Both views come from the same contract row, so they share one currency;
    # the report never mixes currencies or performs implicit FX conversion.
    if contract['currency'] not in {'CNY', 'USD', 'EUR'}:
        reject('variance_currency', 'Unsupported contract currency', currency=contract['currency'])
    totals_b = _meter_totals(view_b)
    totals_a = _meter_totals(view_a)
    if meters is not None:
        unknown = set(meters) - (set(totals_b) | set(totals_a))
        if unknown:
            reject('variance_scope', 'Scoped meter is absent from both periods', meters=sorted(unknown))
    raw_pairs, raw_standalone = _build_alignment(set(totals_b), set(totals_a), meter_align)

    def quantity_string(totals, meter):
        return encoded(_decimal(totals[meter]['quantity_fraction']).quantize(QUANTITY_SCALE, rounding=ROUND_HALF_EVEN))

    comparable, comparable_entries, structural_entries = [], [], []
    for before_meter, after_meter, alignment in raw_pairs:
        q_before = totals_b[before_meter]['quantity_fraction']
        q_after = totals_a[after_meter]['quantity_fraction']
        entry = {
            'before_meter': before_meter,
            'after_meter': after_meter,
            'alignment': alignment,
            'before_cents': totals_b[before_meter]['amount_cents'],
            'after_cents': totals_a[after_meter]['amount_cents'],
            'delta_cents': totals_a[after_meter]['amount_cents'] - totals_b[before_meter]['amount_cents'],
            'before_quantity': quantity_string(totals_b, before_meter),
            'after_quantity': quantity_string(totals_a, after_meter),
            'before_rate_ids': totals_b[before_meter]['rate_ids'],
            'after_rate_ids': totals_a[after_meter]['rate_ids'],
            'rate_changed': totals_b[before_meter]['rate_digests'] != totals_a[after_meter]['rate_digests'],
        }
        if q_before > 0 and q_after > 0:
            entry.update({'status': 'comparable', 'reason': None})
            comparable.append((before_meter, after_meter, alignment))
            comparable_entries.append(entry)
        else:
            entry.update({
                'meter': after_meter,
                'status': 'structural',
                'reason': 'zero_quantity_side',
                'note': '口径不可比：一侧用量为零，保留全部金额差，不构造零价格',
            })
            structural_entries.append(entry)

    for meter, kind in raw_standalone:
        source = totals_a if kind == 'added' else totals_b
        amount = source[meter]['amount_cents']
        structural_entries.append({
            'meter': meter,
            'status': kind,
            'reason': 'new_meter' if kind == 'added' else 'retired_meter',
            'note': '新增计量项，两期未对齐，保留当期金额' if kind == 'added' else '停用计量项，两期未对齐，保留基期金额',
            'before_cents': 0 if kind == 'added' else amount,
            'after_cents': amount if kind == 'added' else 0,
            'delta_cents': amount if kind == 'added' else -amount,
            'quantity': quantity_string(source, meter),
            'rate_ids': source[meter]['rate_ids'],
        })

    # Counterfactual grid over all eight factor coalitions.
    cost_by_coalition, grids = {}, {}
    for coalition in COALITIONS:
        cost, grid = _counterfactual_cost(view_b, view_a, comparable, coalition)
        cost_by_coalition[coalition] = cost
        grids[coalition] = grid
    endpoint_before = cost_by_coalition[EMPTY]
    endpoint_after = cost_by_coalition[FULL]
    frozen_before = sum(totals_b[before_meter]['amount_cents'] for before_meter, _, _ in comparable)
    frozen_after = sum(totals_a[after_meter]['amount_cents'] for _, after_meter, _ in comparable)
    if endpoint_before != frozen_before or endpoint_after != frozen_after:
        reject('variance_endpoint', 'Counterfactual endpoints do not reproduce frozen charges', before=endpoint_before, after=endpoint_after)

    # Per-meter Shapley values, then exact-conservation integer rounding.
    per_meter = {}
    aggregate_exact = {factor: Fraction(0) for factor in FACTORS}
    for pair_index, (before_meter, after_meter, _) in enumerate(comparable):
        meter_cost = {
            coalition: next(row['meter_cents'] for row in grids[coalition] if row['pair_index'] == pair_index)
            for coalition in COALITIONS
        }
        exact = _shapley(meter_cost)
        delta = totals_a[after_meter]['amount_cents'] - totals_b[before_meter]['amount_cents']
        rounded = _largest_remainder(exact, delta)
        if sum(rounded.values()) != delta:
            reject('variance_conservation', 'Rounded meter factors do not sum to meter delta', meter=after_meter)
        for factor in FACTORS:
            aggregate_exact[factor] += exact[factor]
        per_meter[after_meter] = {
            'before_meter': before_meter,
            'after_meter': after_meter,
            'delta_cents': delta,
            'exact': {factor: str(exact[factor]) for factor in FACTORS},
            'contributions_cents': {factor: rounded[factor] for factor in FACTORS},
        }
    comparable_delta = frozen_after - frozen_before
    aggregate_rounded = {
        factor: sum(per_meter[meter]['contributions_cents'][factor] for meter in per_meter)
        for factor in FACTORS
    }
    if sum(aggregate_rounded.values()) != comparable_delta:
        reject('variance_conservation', 'Aggregate factors do not sum to comparable delta')

    scoped_meters = set(meters) if meters else None
    excluded = []

    def in_scope(entry):
        if scoped_meters is None:
            return True
        names = {entry.get('meter'), entry.get('before_meter'), entry.get('after_meter')} - {None}
        return bool(names & scoped_meters)

    scoped_comparable = [entry for entry in comparable_entries if in_scope(entry)]
    scoped_structural = [entry for entry in structural_entries if in_scope(entry)]
    for entry in comparable_entries + structural_entries:
        if not in_scope(entry):
            names = {entry.get('meter'), entry.get('before_meter'), entry.get('after_meter')} - {None}
            excluded.append({'meter': sorted(names)[0], 'before_cents': entry['before_cents'], 'after_cents': entry['after_cents']})
    scoped_comparable_delta = sum(entry['delta_cents'] for entry in scoped_comparable)
    scoped_structural_delta = sum(entry['delta_cents'] for entry in scoped_structural)
    scoped_factor_cents = {
        factor: sum(
            per_meter[entry['after_meter']]['contributions_cents'][factor]
            for entry in scoped_comparable
        )
        for factor in FACTORS
    }

    adjustments_b = _adjustments(store, view_b)
    adjustments_a = _adjustments(store, view_a)
    consumption_delta = view_a['frozen_total_cents'] - view_b['frozen_total_cents']
    structural_delta = sum(entry['delta_cents'] for entry in structural_entries)
    explained = comparable_delta + structural_delta
    grid_output = [
        {
            'coalition': sorted(coalition),
            'factors_present': sorted(coalition),
            'cost_cents': cost_by_coalition[coalition],
            'blocks': grids[coalition],
        }
        for coalition in COALITIONS
    ]
    report = {
        'method': 'shapley_three_factor_symmetric_allocation',
        'factor_order': list(FACTORS),
        'factor_labels': dict(FACTOR_LABELS),
        'permutations': len(list(permutations(FACTORS))),
        'rounding': 'exact_fraction_shapley_with_largest_remainder_to_minor_unit',
        'quantity_scale': '6_decimal_places',
        'contract_id': contract_id,
        'currency': contract['currency'],
        'before_period': before_period,
        'after_period': after_period,
        'scope': {
            'meters': sorted(scoped_meters) if scoped_meters else None,
            'excluded_meters': excluded,
            'note': note,
        },
        'periods': {
            'before': {key: view_b[key] for key in (
                'period', 'head_invoice_id', 'head_digest', 'chain', 'frozen_total_cents',
                'recomputed_total_cents', 'recomputed_matches_frozen', 'rates_fingerprint', 'run_digest')},
            'after': {key: view_a[key] for key in (
                'period', 'head_invoice_id', 'head_digest', 'chain', 'frozen_total_cents',
                'recomputed_total_cents', 'recomputed_matches_frozen', 'rates_fingerprint', 'run_digest')},
        },
        'meter_quantities': {
            'before': {meter: encoded(_decimal(item['quantity_fraction']).quantize(QUANTITY_SCALE, rounding=ROUND_HALF_EVEN)) for meter, item in sorted(totals_b.items())},
            'after': {meter: encoded(_decimal(item['quantity_fraction']).quantize(QUANTITY_SCALE, rounding=ROUND_HALF_EVEN)) for meter, item in sorted(totals_a.items())},
        },
        'alignment': {
            'mapping': meter_align,
            'comparable': comparable_entries,
            'structural': structural_entries,
        },
        'counterfactuals': grid_output,
        'decomposition': {
            'comparable_meters': sorted(per_meter),
            'comparable_delta_cents': comparable_delta,
            'factors': [
                {
                    'factor': factor,
                    'label': FACTOR_LABELS[factor],
                    'exact_fraction': str(aggregate_exact[factor]),
                    'contribution_cents': aggregate_rounded[factor],
                }
                for factor in FACTORS
            ],
            'per_meter': per_meter,
            'structural_delta_cents': structural_delta,
        },
        'scoped_decomposition': {
            'comparable_delta_cents': scoped_comparable_delta,
            'factor_contribution_cents': scoped_factor_cents,
            'structural_delta_cents': scoped_structural_delta,
            'explained_delta_cents': scoped_comparable_delta + scoped_structural_delta,
        },
        'adjustments': {'before': adjustments_b, 'after': adjustments_a},
        'reconciliation': {
            'before_consumption_cents': view_b['frozen_total_cents'],
            'after_consumption_cents': view_a['frozen_total_cents'],
            'consumption_delta_cents': consumption_delta,
            'comparable_delta_cents': comparable_delta,
            'structural_delta_cents': structural_delta,
            'explained_cents': explained,
            'unexplained_cents': consumption_delta - explained,
            'conserved': explained == consumption_delta,
            'chain_bridges_balanced': adjustments_b['bridge_balanced'] and adjustments_a['bridge_balanced'],
        },
    }
    report['digest'] = digest(report)
    return report
