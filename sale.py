# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from decimal import Decimal

from trytond.model import fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval
from trytond.transaction import Transaction

__all__ = ['StockMove', 'Sale', 'SaleLine', 'Move', 'MoveLine']
_ZERO = Decimal('0.0')


# TODO: put it in account_invoice_stock
class StockMove:
    __metaclass__ = PoolMeta
    __name__ = 'stock.move'

    @property
    def posted_quantity(self):
        'The quantity from linked invoice lines in move unit and by invoice'
        pool = Pool()
        Uom = pool.get('product.uom')
        quantity = 0.0
        invoice_quantity = {}
        for invoice_line in self.invoice_lines:
            if (invoice_line.invoice and
                    invoice_line.invoice.state in ('posted', 'paid')):
                if invoice_line.invoice.id not in invoice_quantity:
                    invoice_quantity[invoice_line.invoice.id] = 0.0
                quantity = Uom.compute_qty(invoice_line.unit,
                    invoice_line.quantity, self.uom)
                invoice_quantity[invoice_line.invoice.id] += quantity
        return invoice_quantity


class Move:
    __metaclass__ = PoolMeta
    __name__ = 'account.move'

    @classmethod
    def _get_origin(cls):
        origins = super(Move, cls)._get_origin()
        if 'sale.sale' not in origins:
            origins.append('sale.sale')
        return origins


class MoveLine:
    __metaclass__ = PoolMeta
    __name__ = 'account.move.line'
    sale_line = fields.Many2One('sale.line', 'Sale Line')


class Sale:
    __metaclass__ = PoolMeta
    __name__ = 'sale.sale'

    @classmethod
    def __setup__(cls):
        super(Sale, cls).__setup__()
        cls._error_messages.update({
                'no_pending_invoice_account': ('There is no Pending Invoice '
                    'Account Defined. Please define one in sale '
                    'configuration.'),
                })

    @classmethod
    def process(cls, sales):
        super(Sale, cls).process(sales)
        for sale in sales:
            sale.create_stock_account_move()

    def create_stock_account_move(self):
        """
        Create, post and reconcile an account_move (if it is required to do)
        with lines related to Pending Invoices accounts.
        """
        pool = Pool()
        Config = pool.get('sale.configuration')
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')

        if self.invoice_method != 'shipment':
            return

        config = Config(1)
        if not config.pending_invoice_account:
            self.raise_user_error('no_pending_invoice_account')

        with Transaction().set_context(_check_access=False):
            account_move = self._get_stock_account_move(
                config.pending_invoice_account)
            if account_move:
                account_move.save()
                Move.post([account_move])

                to_reconcile = MoveLine.search([
                            ('move.origin', '=', str(self)),
                            ('account', '=', config.pending_invoice_account),
                            ('reconciliation', '=', None),
                            ['OR',
                                # previous pending line
                                ('move', '!=', account_move),
                                # current move "to reconcile line"
                                ('sale_line', '=', None),
                                ],
                            ])
                credit = sum(l.credit for l in to_reconcile)
                debit = sum(l.debit for l in to_reconcile)
                if to_reconcile and credit == debit:
                    MoveLine.reconcile(to_reconcile)

    def _get_stock_account_move(self, pending_invoice_account):
        "Return the account move for shipped quantities"
        pool = Pool()
        Date = pool.get('ir.date')
        Move = pool.get('account.move')
        Period = pool.get('account.period')

        if self.invoice_method in ['manual', 'order']:
            return

        move_lines = []
        for line in self.lines:
            move_lines += line._get_stock_account_move_lines(
                pending_invoice_account)
        if not move_lines:
            return

        accounting_date = Date().today()
        period_id = Period.find(self.company.id, date=accounting_date)
        return Move(
            origin=self,
            period=period_id,
            journal=self._get_accounting_journal(),
            date=accounting_date,
            lines=move_lines,
            )

    def _get_accounting_journal(self):
        pool = Pool()
        Journal = pool.get('account.journal')
        journals = Journal.search([
                ('type', '=', 'revenue'),
                ], limit=1)
        if journals:
            journal, = journals
        else:
            journal = None
        return journal


class SaleLine:
    __metaclass__ = PoolMeta
    __name__ = 'sale.line'

    analytic_required = fields.Function(fields.Boolean("Require Analytics"),
        'on_change_with_analytic_required')

    @classmethod
    def __setup__(cls):
        super(SaleLine, cls).__setup__()
        if hasattr(cls, 'analytic_accounts'):
            if not cls.analytic_accounts.states:
                cls.analytic_accounts.states = {}
            if cls.analytic_accounts.states.get('required'):
                cls.analytic_accounts.states['required'] |= (
                    Eval('analytic_required', False))
            else:
                cls.analytic_accounts.states['required'] = (
                    Eval('analytic_required', False))

    @fields.depends('product')
    def on_change_with_analytic_required(self, name=None):
        if not hasattr(self, 'analytic_accounts') or not self.product:
            return False

        if getattr(self.product.account_revenue_used, 'analytic_required',
                    False):
            return True
        return False

    def _get_stock_account_move_lines(self, pending_invoice_account):
        """
        Return the account move lines for shipped quantities and
        to reconcile shipped and invoiced (and posted) quantities
        """
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        Currency = pool.get('currency.currency')

        if (not self.product or self.product.type == 'service' or
                not self.moves):
            # Sale Line not shipped
            return []

        unposted_shiped_quantity = self._get_unposted_shiped_quantity()

        # Previously created stock account move lines (pending to invoice
        # amount)
        lines_to_reconcile = MoveLine.search([
                    ('sale_line', '=', self),
                    ('account', '=', pending_invoice_account),
                    ('reconciliation', '=', None),
                    ])

        move_lines = []
        if not unposted_shiped_quantity and not lines_to_reconcile:
            return move_lines

        # Reconcile previously created (and not yet reconciled)
        # stock account move lines: all or partialy invoiced now
        # it use amount  because it has been created using quantities and
        # sale line unit price => it is reliable
        amount_to_reconcile = sum(l.debit - l.credit
            for l in lines_to_reconcile) if lines_to_reconcile else _ZERO
        if amount_to_reconcile != _ZERO:
            to_reconcile_line = MoveLine()
            to_reconcile_line.account = pending_invoice_account
            if to_reconcile_line.account.party_required:
                to_reconcile_line.party = self.sale.party
            if amount_to_reconcile > _ZERO:
                to_reconcile_line.credit = amount_to_reconcile
                to_reconcile_line.debit = _ZERO
            else:
                to_reconcile_line.debit = abs(amount_to_reconcile)
                to_reconcile_line.credit = _ZERO
            to_reconcile_line.reconciliation = None
            move_lines.append(to_reconcile_line)

        pending_amount = Currency.compute(self.sale.company.currency,
            Decimal(unposted_shiped_quantity) * self.unit_price,
            self.sale.currency) if unposted_shiped_quantity else _ZERO

        if amount_to_reconcile == _ZERO and unposted_shiped_quantity:
            # no previous amount in pending invoice account nor pending to
            # invoice (and post) quantity => first time
            invoiced_amount = -pending_amount
        elif not unposted_shiped_quantity:
            # no pending to invoice and post quantity => invoiced all shiped
            if amount_to_reconcile > _ZERO:
                invoiced_amount = amount_to_reconcile
            else:
                invoiced_amount = -amount_to_reconcile
        else:
            # invoiced partially shiped quantity
            invoiced_amount = -(amount_to_reconcile + pending_amount)

        if pending_amount == amount_to_reconcile:
            return []

        if invoiced_amount != _ZERO:
            invoiced_line = MoveLine()
            invoiced_line.account = self.product.account_revenue_used
            if invoiced_line.account.party_required:
                invoiced_line.party = self.sale.party
            invoiced_line.sale_line = self
            if invoiced_amount > _ZERO:
                invoiced_line.debit = invoiced_amount
                invoiced_line.credit = _ZERO
            else:
                invoiced_line.credit = abs(invoiced_amount)
                invoiced_line.debit = _ZERO
            self._set_analytic_lines(invoiced_line)
            move_lines.append(invoiced_line)

        if pending_amount != _ZERO:
            pending_line = MoveLine()
            pending_line.account = pending_invoice_account
            if pending_line.account.party_required:
                pending_line.party = self.sale.party
            pending_line.sale_line = self
            if pending_amount > _ZERO:
                pending_line.debit = pending_amount
                pending_line.credit = _ZERO
            else:
                pending_line.credit = abs(pending_amount)
                pending_line.debit = _ZERO
            move_lines.append(pending_line)

        return move_lines

    def _get_unposted_shiped_quantity(self):
        """
        Returns the shipped quantity which is not invoiced and posted
        """
        pool = Pool()
        Uom = pool.get('product.uom')

        sign = -1 if self.quantity < 0.0 else 1
        posted_quantity = 0.0
        sended_quantity = 0.0
        invoice_quantity = {}
        for move in self.moves:
            if move.state != 'done':
                continue
            sended_quantity += move.quantity
            for invoice, quantity in move.posted_quantity.iteritems():
                if invoice not in invoice_quantity:
                    invoice_quantity[invoice] = quantity
                else:
                    invoice_quantity[invoice] += quantity
        posted_quantity = sum(invoice_quantity.values())
        # in case split moves, posted quantity is greater than purchase quantity
        if posted_quantity > self.quantity:
            posted_quantity = self.quantity
        return sign * Uom.compute_qty(move.uom,
            sended_quantity - posted_quantity, self.unit)

    def _set_analytic_lines(self, move_line):
        """
        Add to supplied account move line analytic lines based on sale line
        analytic accounts value
        """
        pool = Pool()
        Date = pool.get('ir.date')

        if (not getattr(self, 'analytic_accounts', False) or
                not self.analytic_accounts):
            return []

        AnalyticLine = pool.get('analytic_account.line')
        analytic_lines = []
        for entry in self.analytic_accounts:
            line = AnalyticLine()
            analytic_lines.append(line)

            line.name = self.description
            line.debit = move_line.debit
            line.credit = move_line.credit
            line.account = entry.account
            line.journal = self.sale._get_accounting_journal()
            line.date = Date.today()
            line.reference = self.sale.reference
            line.party = self.sale.party
        move_line.analytic_lines = analytic_lines
